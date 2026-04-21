import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';
import { AlarmTopic } from '../constructs/alarm-topic';

interface IProps extends cdk.StackProps {
  readonly notificationEmail: string;
}

export class EventBusStack extends cdk.Stack {
  readonly alarmTopic: sns.ITopic;
  readonly alarmQueue: sqs.IQueue;
  readonly deadLetterQueue: sqs.IQueue;

  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;

    const alarmTopicConstruct = new AlarmTopic(
      this,
      'AlarmTopicConstruct',
      props,
    );
    this.alarmTopic = alarmTopicConstruct.topic;

    this.deadLetterQueue = this.newDeadLetterQueue(ns);
    this.alarmQueue = this.newAlarmQueue(ns);
  }

  private newDeadLetterQueue(ns: string): sqs.Queue {
    return new sqs.Queue(this, 'DeadLetterQueue', {
      queueName: `${ns}DeadLetterQueue`,
      visibilityTimeout: cdk.Duration.minutes(10),
      retentionPeriod: cdk.Duration.days(14),
    });
  }

  private newAlarmQueue(ns: string): sqs.Queue {
    const queue = new sqs.Queue(this, 'AlarmQueue', {
      queueName: `${ns}AlarmQueue`,
      visibilityTimeout: cdk.Duration.minutes(25),
      retentionPeriod: cdk.Duration.days(4),
      deadLetterQueue: {
        queue: this.deadLetterQueue,
        maxReceiveCount: 3,
      },
    });

    this.alarmTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(queue, {
        rawMessageDelivery: true,
      }),
    );

    return queue;
  }
}
