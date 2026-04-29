import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly alarmTopic: sns.ITopic;
  readonly notificationTopic: sns.ITopic;
  readonly rcaSessionTable: dynamodb.ITable;
  readonly evidenceBucket: s3.IBucket;
  readonly vectorBucketName: string;
  readonly reportBucket: string;
  readonly imageTag: string;
}

export class CcHeadlessStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;

    const deadLetterQueue = this.newDeadLetterQueue(ns);
    const alarmQueue = this.newAlarmQueue(ns, props.alarmTopic, deadLetterQueue);
    const cluster = this.newCluster(ns, props.vpc);
    const taskDefinition = this.newTaskDefinition(ns, props, alarmQueue);
    this.newService(ns, cluster, taskDefinition);
  }

  private newDeadLetterQueue(ns: string): sqs.Queue {
    return new sqs.Queue(this, 'DeadLetterQueue', {
      queueName: `${ns}CcHeadlessDLQ`,
      visibilityTimeout: cdk.Duration.minutes(10),
      retentionPeriod: cdk.Duration.days(14),
    });
  }

  private newAlarmQueue(
    ns: string,
    alarmTopic: sns.ITopic,
    deadLetterQueue: sqs.Queue,
  ): sqs.Queue {
    const queue = new sqs.Queue(this, 'AlarmQueue', {
      queueName: `${ns}CcHeadlessQueue`,
      visibilityTimeout: cdk.Duration.minutes(35),
      retentionPeriod: cdk.Duration.days(4),
      deadLetterQueue: {
        queue: deadLetterQueue,
        maxReceiveCount: 3,
      },
    });

    alarmTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(queue, {
        rawMessageDelivery: true,
      }),
    );

    return queue;
  }

  private newCluster(ns: string, vpc: ec2.IVpc): ecs.Cluster {
    return new ecs.Cluster(this, 'Cluster', {
      clusterName: `${ns}CcHeadless`,
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });
  }

  private newTaskDefinition(
    ns: string,
    props: IProps,
    alarmQueue: sqs.Queue,
  ): ecs.FargateTaskDefinition {
    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      family: `${ns}CcHeadless`,
      cpu: 1024,
      memoryLimitMiB: 2048,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${ns}/cc-headless`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const githubPatSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'GithubPatSecret',
      `${ns}/github/pat`,
    );

    taskDef.addContainer('CcHeadless', {
      containerName: 'cc-headless',
      image: ecs.ContainerImage.fromRegistry(
        `${cdk.Aws.ACCOUNT_ID}.dkr.ecr.${cdk.Aws.REGION}.amazonaws.com/${ns.toLowerCase()}/cc-headless:${props.imageTag}`,
      ),
      essential: true,
      stopTimeout: cdk.Duration.seconds(120),
      environment: {
        AWS_REGION: cdk.Aws.REGION,
        CLAUDE_CODE_USE_BEDROCK: '1',
        ANTHROPIC_DEFAULT_SONNET_MODEL: 'global.anthropic.claude-sonnet-4-6[1m]',
        SQS_QUEUE_URL: alarmQueue.queueUrl,
        DYNAMODB_TABLE_NAME: props.rcaSessionTable.tableName,
        S3_EVIDENCE_BUCKET: props.evidenceBucket.bucketName,
        S3_VECTOR_BUCKET_NAME: props.vectorBucketName,
        S3_REPORT_BUCKET: props.reportBucket,
        SNS_NOTIFICATION_TOPIC_ARN: props.notificationTopic.topicArn,
      },
      secrets: {
        GITHUB_PERSONAL_ACCESS_TOKEN: ecs.Secret.fromSecretsManager(githubPatSecret),
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'cc-headless',
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

    this.grantTaskPermissions(taskDef, props, alarmQueue);
    this.grantEcrPull(taskDef);

    return taskDef;
  }

  private grantTaskPermissions(
    taskDef: ecs.FargateTaskDefinition,
    props: IProps,
    alarmQueue: sqs.Queue,
  ): void {
    alarmQueue.grantConsumeMessages(taskDef.taskRole);

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

    taskDef.taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('AWSCloudTrail_ReadOnlyAccess'),
    );

    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['sns:Publish'],
        resources: [props.notificationTopic.topicArn],
      }),
    );

    if (props.reportBucket) {
      taskDef.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ['s3:PutObject', 's3:GetObject'],
          resources: [
            `arn:aws:s3:::${props.reportBucket}`,
            `arn:aws:s3:::${props.reportBucket}/*`,
          ],
        }),
      );
    }

    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          'ecs:UpdateService',
          'ecs:DescribeServices',
        ],
        resources: ['*'],
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
      serviceName: `${ns}CcHeadless`,
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
