import * as cdk from 'aws-cdk-lib'
import * as iam from 'aws-cdk-lib/aws-iam'
import * as sns from 'aws-cdk-lib/aws-sns'
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions'
import { Construct } from 'constructs'

interface IProps extends cdk.StackProps {
  readonly notificationEmail: string
}

export class AlarmTopic extends Construct {
  readonly topic: sns.ITopic

  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id)

    const ns = this.node.tryGetContext('ns') as string

    const topic = new sns.Topic(this, 'AlarmTopic', {
      topicName: `${ns}Alarm`,
      displayName: 'CloudWatch Alarm → RCA Agent',
      enforceSSL: true,
      tracingConfig: sns.TracingConfig.ACTIVE,
    })

    topic.addSubscription(new snsSubscriptions.EmailSubscription(props.notificationEmail))

    topic.addToResourcePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [topic.topicArn],
      })
    )

    this.topic = topic
  }
}
