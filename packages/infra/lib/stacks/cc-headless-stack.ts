import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly alarmTopic: sns.ITopic;
  readonly notificationTopic: sns.ITopic;
  readonly rcaSessionTable: dynamodb.ITable;
  readonly evidenceBucket: s3.IBucket;
  readonly vectorBucketName: string;
  readonly reportBucket: string;
  readonly repository: ecr.IRepository;
  readonly imageTag: string;
}

export class CcHeadlessStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;

    const deadLetterQueue = this.newDeadLetterQueue(ns);
    const alarmQueue = this.newAlarmQueue(ns, props.alarmTopic, deadLetterQueue);
    const fn = this.newLambdaFunction(ns, props, alarmQueue);
    this.grantPermissions(fn, props);
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
      visibilityTimeout: cdk.Duration.minutes(16),
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

  private newLambdaFunction(
    ns: string,
    props: IProps,
    alarmQueue: sqs.Queue,
  ): lambda.Function {
    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/lambda/${ns}-cc-headless`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const fn = new lambda.DockerImageFunction(this, 'Function', {
      functionName: `${ns}CcHeadless`,
      code: lambda.DockerImageCode.fromEcr(props.repository, {
        tagOrDigest: props.imageTag,
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 2048,
      timeout: cdk.Duration.minutes(15),
      reservedConcurrentExecutions: 1,
      ephemeralStorageSize: cdk.Size.mebibytes(512),
      environment: {
        CLAUDE_CODE_USE_BEDROCK: '1',
        ANTHROPIC_DEFAULT_SONNET_MODEL: 'global.anthropic.claude-sonnet-4-6',
        DYNAMODB_TABLE_NAME: props.rcaSessionTable.tableName,
        S3_EVIDENCE_BUCKET: props.evidenceBucket.bucketName,
        S3_VECTOR_BUCKET_NAME: props.vectorBucketName,
        S3_REPORT_BUCKET: props.reportBucket,
        SNS_NOTIFICATION_TOPIC_ARN: props.notificationTopic.topicArn,
      },
      logGroup,
    });

    fn.addEventSource(
      new SqsEventSource(alarmQueue, {
        batchSize: 1,
      }),
    );

    return fn;
  }

  private grantPermissions(
    fn: lambda.Function,
    props: IProps,
  ): void {
    props.rcaSessionTable.grantReadWriteData(fn);

    props.evidenceBucket.grantReadWrite(fn);

    fn.addToRolePolicy(
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

    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
        ],
        resources: ['*'],
      }),
    );

    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'cloudwatch:GetMetricData',
          'cloudwatch:ListMetrics',
          'cloudwatch:DescribeAlarms',
        ],
        resources: ['*'],
      }),
    );

    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'logs:StartQuery',
          'logs:GetQueryResults',
          'logs:StopQuery',
          'logs:DescribeLogGroups',
        ],
        resources: ['*'],
      }),
    );

    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['cloudtrail:LookupEvents'],
        resources: ['*'],
      }),
    );

    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['sns:Publish'],
        resources: [props.notificationTopic.topicArn],
      }),
    );

    if (props.reportBucket) {
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['s3:PutObject', 's3:GetObject'],
          resources: [
            `arn:aws:s3:::${props.reportBucket}`,
            `arn:aws:s3:::${props.reportBucket}/*`,
          ],
        }),
      );
    }
  }
}
