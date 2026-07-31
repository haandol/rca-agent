import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly notificationEmail: string;
}

export class AlarmTopic extends Construct {
  readonly topic: sns.ITopic;

  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id);

    const ns = this.node.tryGetContext('ns') as string;

    const topic = new sns.Topic(this, 'AlarmTopic', {
      topicName: `${ns}Alarm`,
      displayName: 'CloudWatch Alarm → RCA Agent',
      enforceSSL: true,
      tracingConfig: sns.TracingConfig.ACTIVE,
    });

    topic.addSubscription(
      new snsSubscriptions.EmailSubscription(props.notificationEmail),
    );

    // Allowing the CloudWatch service principal alone would let an alarm in any
    // account publish here, and one alarm starts a full run of both analysis
    // engines. Alarm ingestion is unauthenticated, so requiring that the request
    // came from this account and from an alarm this deployment owns is the only
    // boundary between an outside alarm and unbounded model spend.
    const stack = cdk.Stack.of(this);
    topic.addToResourcePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [topic.topicArn],
        conditions: {
          StringEquals: { 'aws:SourceAccount': stack.account },
          ArnLike: {
            'aws:SourceArn': stack.formatArn({
              service: 'cloudwatch',
              resource: 'alarm',
              // Alarms live in the same account but in sibling stacks, so the
              // grant is scoped by this deployment's alarm-name prefix rather
              // than by a list of alarm ARNs that would couple this construct to
              // every stack that adds one.
              resourceName: `${ns}-*`,
              arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
            }),
          },
        },
      }),
    );

    this.topic = topic;
  }
}
