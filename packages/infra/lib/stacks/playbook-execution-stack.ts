import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { denyApprovalSnapshotMutation } from '../constructs/approval-snapshot-access';
import { grantEcrPull } from '../constructs/ecr-access';
import {
  healthcareExecutionRoleName,
  healthcareTaskRoleName,
} from '../constructs/healthcare-role-names';

interface IProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly healthcareService: ecs.FargateService;
  readonly rcaSessionTable: dynamodb.ITable;
  readonly evidenceBucket: s3.IBucket;
  readonly vectorBucketName: string;
  readonly imageTag: string;
}

const HEALTHCARE_PORT = 8000;

// Execution waits for the success criteria to become observable, so it can run
// long. The claim has to outlive the worst case or an in-flight request gets
// redelivered and runs a second time; the queue's visibility timeout carries the
// same bound.
const EXECUTION_TIMEOUT_SECONDS = 3600;
const EXECUTION_VISIBILITY_TIMEOUT_SECONDS = EXECUTION_TIMEOUT_SECONDS + 900;

/**
 * Runs playbook executions a person has approved.
 *
 * Approval *is* the message: the only way into this stack is a request the
 * dashboard publishes to its queue. There is no event subscription here, so no
 * path exists for an execution to start without a person asking for it.
 *
 * This is also the only task role in the system with write permission. The
 * analysis engines stay read-only, and the two never share a role — otherwise a
 * defect in the analysis path would reach write access.
 */
export class PlaybookExecutionStack extends cdk.Stack {
  public readonly requestQueue: sqs.Queue;

  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;

    const deadLetterQueue = this.newDeadLetterQueue(ns);
    this.requestQueue = this.newRequestQueue(ns, deadLetterQueue);
    const cluster = this.newCluster(ns, props.vpc);
    const taskDefinition = this.newTaskDefinition(ns, props, this.requestQueue);
    const service = this.newService(ns, cluster, taskDefinition);

    // Unlike the analysis engines, execution reaches the target service itself.
    props.healthcareService.connections.allowFrom(
      service,
      ec2.Port.tcp(HEALTHCARE_PORT),
      'Playbook execution acting on the Healthcare service',
    );
  }

  private newDeadLetterQueue(ns: string): sqs.Queue {
    return new sqs.Queue(this, 'DeadLetterQueue', {
      queueName: `${ns}PlaybookExecutionDLQ`,
      visibilityTimeout: cdk.Duration.minutes(10),
      retentionPeriod: cdk.Duration.days(14),
    });
  }

  private newRequestQueue(ns: string, deadLetterQueue: sqs.Queue): sqs.Queue {
    // No subscription: a message here means a person approved an execution.
    return new sqs.Queue(this, 'RequestQueue', {
      queueName: `${ns}PlaybookExecutionQueue`,
      visibilityTimeout: cdk.Duration.seconds(
        EXECUTION_VISIBILITY_TIMEOUT_SECONDS,
      ),
      retentionPeriod: cdk.Duration.days(4),
      deadLetterQueue: {
        queue: deadLetterQueue,
        maxReceiveCount: 3,
      },
    });
  }

  private newCluster(ns: string, vpc: ec2.IVpc): ecs.Cluster {
    return new ecs.Cluster(this, 'Cluster', {
      clusterName: `${ns}PlaybookExecution`,
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });
  }

  private newTaskDefinition(
    ns: string,
    props: IProps,
    requestQueue: sqs.Queue,
  ): ecs.FargateTaskDefinition {
    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      family: `${ns}PlaybookExecution`,
      cpu: 1024,
      memoryLimitMiB: 2048,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${ns}/playbook-execution`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    taskDef.addContainer('PlaybookExecution', {
      containerName: 'playbook-execution',
      // The same image as the analysis worker: one harness, two entry points.
      image: ecs.ContainerImage.fromRegistry(
        `${cdk.Aws.ACCOUNT_ID}.dkr.ecr.${cdk.Aws.REGION}.amazonaws.com/${ns.toLowerCase()}/cc-headless:${props.imageTag}`,
      ),
      command: ['python', '-m', 'headless_codex.execution_main'],
      essential: true,
      stopTimeout: cdk.Duration.seconds(120),
      environment: {
        AWS_REGION: cdk.Aws.REGION,
        CODEX_MODEL: 'global.openai.gpt-5.6-sol',
        CODEX_REASONING_EFFORT: 'high',
        CODEX_MODEL_PROVIDER: 'amazon-bedrock-runtime',
        CODEX_BEDROCK_BASE_URL: `https://bedrock-runtime.${cdk.Aws.REGION}.amazonaws.com/openai/v1`,
        EXECUTION_QUEUE_URL: requestQueue.queueUrl,
        EXECUTION_TIMEOUT_SECONDS: String(EXECUTION_TIMEOUT_SECONDS),
        DYNAMODB_TABLE_NAME: props.rcaSessionTable.tableName,
        S3_EVIDENCE_BUCKET: props.evidenceBucket.bucketName,
        S3_VECTOR_BUCKET_NAME: props.vectorBucketName,
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'playbook-execution',
        logGroup,
      }),
      healthCheck: {
        command: [
          'CMD-SHELL',
          'node -e "fetch(\'http://localhost:8080/healthz\').then(r=>{if(!r.ok)throw 1}).catch(()=>process.exit(1))" || exit 1',
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        startPeriod: cdk.Duration.seconds(30),
        retries: 3,
      },
      portMappings: [{ containerPort: 8080 }],
    });

    this.grantTaskPermissions(ns, taskDef, props, requestQueue);
    grantEcrPull(taskDef);

    return taskDef;
  }

  private grantTaskPermissions(
    ns: string,
    taskDef: ecs.FargateTaskDefinition,
    props: IProps,
    requestQueue: sqs.Queue,
  ): void {
    requestQueue.grantConsumeMessages(taskDef.taskRole);
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.DENY,
        actions: ['sqs:SendMessage'],
        resources: [requestQueue.queueArn],
      }),
    );

    props.rcaSessionTable.grantReadWriteData(taskDef.taskRole);

    // Execution evidence and the pre-execution playbook copy land here.
    props.evidenceBucket.grantReadWrite(taskDef.taskRole);
    denyApprovalSnapshotMutation(taskDef.taskRole, props.evidenceBucket);

    // The retrospective reindexes the playbook it revised so the next similar
    // incident retrieves the corrected procedure.
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          's3vectors:GetIndex',
          's3vectors:PutVectors',
          's3vectors:GetVectors',
          's3vectors:QueryVectors',
        ],
        resources: [
          `arn:aws:s3vectors:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:bucket/${props.vectorBucketName}`,
          `arn:aws:s3vectors:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:bucket/${props.vectorBucketName}/*`,
        ],
      }),
    );

    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock:CallWithBearerToken',
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
        ],
        resources: ['*'],
      }),
    );

    // Observing whether a step met its success criteria.
    taskDef.taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchReadOnlyAccess'),
    );

    this.grantExecutionWritePermissions(ns, taskDef);
  }

  /**
   * Grants the broad write permission execution needs, minus the scopes that
   * are never an execution target.
   *
   * Target resources are deliberately unrestricted: constraining them by ARN
   * would put the playbook's expressiveness back inside an allowlist, which is
   * what moving the execution basis to the playbook was meant to avoid.
   *
   * **Destructive actions are refused by the execution tool, not by this
   * policy.** Operation-name vocabulary and IAM action names do not map
   * one-to-one, so expressing the refusal list here would leave gaps, and a
   * policy denial cannot record why a step was blocked or mark that step a
   * manual action. The tool can do both, so the policy is only the coarse
   * boundary: it carves out the scopes that are never an execution target.
   *
   * PowerUserAccess already withholds IAM mutation, Organizations, and account
   * management. The explicit deny adds the billing and identity-store scopes it
   * leaves reachable, and makes the boundary readable in the template rather
   * than implied by a managed policy's contents.
   */
  private grantExecutionWritePermissions(
    ns: string,
    taskDef: ecs.FargateTaskDefinition,
  ): void {
    taskDef.taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('PowerUserAccess'),
    );

    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.DENY,
        actions: [
          'organizations:*',
          'account:*',
          'billing:*',
          'budgets:*',
          'ce:*',
          'cur:*',
          'aws-portal:*',
          'sso:*',
          'sso-directory:*',
          'identitystore:*',
          // Granting or revoking access is never a recovery step.
          'iam:Create*',
          'iam:Delete*',
          'iam:Update*',
          'iam:Put*',
          'iam:Attach*',
          'iam:Detach*',
          'iam:Add*',
          'iam:Remove*',
          'iam:Set*',
        ],
        resources: ['*'],
      }),
    );

    // Rolling a service back to a known-good task definition is an allowed
    // recovery action, and ECS cannot accept one without being handed that
    // definition's roles. Without this the most direct recovery path fails on
    // AccessDenied while force-new-deployment succeeds — observed in a live run.
    //
    // The pass is confined to ECS tasks. Passing a role to an arbitrary service
    // would be a privilege-escalation path, and no recovery step needs one.
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['iam:PassRole'],
        resources: [
          this.formatArn({
            service: 'iam',
            region: '',
            resource: 'role',
            resourceName: healthcareTaskRoleName(ns),
          }),
          this.formatArn({
            service: 'iam',
            region: '',
            resource: 'role',
            resourceName: healthcareExecutionRoleName(ns),
          }),
        ],
        conditions: {
          StringEquals: { 'iam:PassedToService': 'ecs-tasks.amazonaws.com' },
        },
      }),
    );
  }

  private newService(
    ns: string,
    cluster: ecs.Cluster,
    taskDefinition: ecs.FargateTaskDefinition,
  ): ecs.FargateService {
    // Always on. Gating the feature by task count made sense when the trigger
    // was an event subscription; with an approval gate the queue already decides
    // whether anything runs.
    return new ecs.FargateService(this, 'Service', {
      serviceName: `${ns}PlaybookExecution`,
      cluster,
      taskDefinition,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      minHealthyPercent: 100,
      circuitBreaker: { enable: true, rollback: true },
      enableExecuteCommand: true,
    });
  }
}
