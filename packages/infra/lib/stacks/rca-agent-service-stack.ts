import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly alarmQueue: sqs.IQueue;
  readonly alarmTopic: sns.ITopic;
  readonly rcaSessionTable: dynamodb.ITable;
  readonly evidenceBucket: s3.IBucket;
  readonly vectorBucketName: string;
  readonly imageTag: string;
  readonly tracing: boolean;
}

export class RcaAgentServiceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;

    const cluster = this.newCluster(ns, props.vpc);
    const taskDefinition = this.newTaskDefinition(ns, props);
    this.newService(ns, cluster, taskDefinition);
  }

  private newCluster(ns: string, vpc: ec2.IVpc): ecs.Cluster {
    return new ecs.Cluster(this, 'Cluster', {
      clusterName: `${ns}RcaAgent`,
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });
  }

  private newTaskDefinition(
    ns: string,
    props: IProps,
  ): ecs.FargateTaskDefinition {
    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      family: `${ns}RcaAgent`,
      cpu: 1024,
      memoryLimitMiB: 2048,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${ns}/rca-agent`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const githubPatSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'GithubPatSecret',
      `${ns}/github/pat`,
    );

    taskDef.addContainer('RcaAgent', {
      containerName: 'rca-agent',
      image: ecs.ContainerImage.fromRegistry(
        `${cdk.Aws.ACCOUNT_ID}.dkr.ecr.${cdk.Aws.REGION}.amazonaws.com/${ns.toLowerCase()}/rca-agent:${props.imageTag}`,
      ),
      essential: true,
      environment: {
        AWS_REGION: cdk.Aws.REGION,
        SQS_QUEUE_URL: props.alarmQueue.queueUrl,
        SNS_NOTIFICATION_TOPIC_ARN: props.alarmTopic.topicArn,
        DYNAMODB_TABLE_NAME: props.rcaSessionTable.tableName,
        S3_EVIDENCE_BUCKET: props.evidenceBucket.bucketName,
        S3_VECTOR_BUCKET_NAME: props.vectorBucketName,
        S3_REPORT_BUCKET: props.evidenceBucket.bucketName,
        OTEL_SERVICE_NAME: 'rca-agent',
        FAULT_DB_LEAK: 'false',
        FAULT_SLOW_QUERY_MS: '0',
        FAULT_ERROR_RATE: '0.0',
      },
      secrets: {
        GITHUB_PERSONAL_ACCESS_TOKEN: ecs.Secret.fromSecretsManager(githubPatSecret),
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'rca-agent',
        logGroup,
      }),
      healthCheck: {
        command: [
          'CMD-SHELL',
          'python -c "import urllib.request; urllib.request.urlopen(\'http://localhost:8000/healthz\')" || exit 1',
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        startPeriod: cdk.Duration.seconds(30),
        retries: 3,
      },
      portMappings: [{ containerPort: 8000 }],
    });

    if (props.tracing) {
      taskDef.addContainer('OtelCollector', {
        containerName: 'otel-collector',
        image: ecs.ContainerImage.fromRegistry(
          'public.ecr.aws/aws-observability/aws-otel-collector:latest',
        ),
        essential: false,
        logging: ecs.LogDrivers.awsLogs({
          streamPrefix: 'otel-collector',
          logGroup,
        }),
        portMappings: [{ containerPort: 4317 }, { containerPort: 4318 }],
      });
    }

    this.grantTaskPermissions(taskDef, props);
    this.grantEcrPull(taskDef);

    return taskDef;
  }

  private grantTaskPermissions(
    taskDef: ecs.FargateTaskDefinition,
    props: IProps,
  ): void {
    props.alarmQueue.grantConsumeMessages(taskDef.taskRole);

    props.rcaSessionTable.grantReadWriteData(taskDef.taskRole);

    props.evidenceBucket.grantReadWrite(taskDef.taskRole);

    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          's3vectors:CreateIndex',
          's3vectors:GetIndex',
          's3vectors:ListIndexes',
          's3vectors:PutVectors',
          's3vectors:GetVectors',
          's3vectors:DeleteVectors',
          's3vectors:QueryVectors',
          's3vectors:ListVectors',
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
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
        ],
        resources: ['*'],
      }),
    );

    taskDef.taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchReadOnlyAccess'),
    );

    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          'xray:BatchGetTraces',
          'xray:GetTraceSummaries',
          'xray:PutTraceSegments',
          'xray:PutTelemetryRecords',
        ],
        resources: ['*'],
      }),
    );

    taskDef.taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('AWSCloudTrail_ReadOnlyAccess'),
    );

    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['sns:Publish'],
        resources: [props.alarmTopic.topicArn],
      }),
    );
  }

  private grantEcrPull(taskDef: ecs.FargateTaskDefinition): void {
    taskDef.executionRole!.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName(
        'AmazonEC2ContainerRegistryReadOnly',
      ),
    );
  }

  private newService(
    ns: string,
    cluster: ecs.Cluster,
    taskDefinition: ecs.FargateTaskDefinition,
  ): ecs.FargateService {
    return new ecs.FargateService(this, 'Service', {
      serviceName: `${ns}RcaAgent`,
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
