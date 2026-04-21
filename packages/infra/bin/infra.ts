import 'source-map-support/register'
import * as cdk from 'aws-cdk-lib'
import { NetworkStack } from '../lib/stacks/network-stack'
import { EventBusStack } from '../lib/stacks/event-bus-stack'
import { DatabaseStack } from '../lib/stacks/database-stack'
import { StorageStack } from '../lib/stacks/storage-stack'
import { RcaAgentServiceStack } from '../lib/stacks/rca-agent-service-stack'
import { Config } from '../config/loader'

const app = new cdk.App({
  context: {
    ns: Config.app.ns,
    stage: Config.app.stage,
  },
})

const env = {
  region: Config.aws.region,
  account: process.env.CDK_DEFAULT_ACCOUNT,
}

const networkStack = new NetworkStack(app, `${Config.app.ns}NetworkStack`, { env })

const eventBusStack = new EventBusStack(app, `${Config.app.ns}EventBusStack`, {
  env,
  notificationEmail: Config.alarm.notificationEmail,
})

const databaseStack = new DatabaseStack(app, `${Config.app.ns}DatabaseStack`, {
  env,
  rcaSessionTableName: Config.table.rcaSession.name,
})

const storageStack = new StorageStack(app, `${Config.app.ns}StorageStack`, {
  env,
  evidenceBucketName: Config.storage.evidenceBucket,
  vectorBucketName: Config.storage.vectorBucket,
})

const rcaAgentServiceStack = new RcaAgentServiceStack(app, `${Config.app.ns}RcaAgentServiceStack`, {
  env,
  vpc: networkStack.vpc,
  alarmQueue: eventBusStack.alarmQueue,
  alarmTopic: eventBusStack.alarmTopic,
  rcaSessionTable: databaseStack.rcaSessionTable,
  evidenceBucket: storageStack.evidenceBucket,
  vectorBucketName: Config.storage.vectorBucket,
  imageTag: Config.agent.imageTag,
  tracing: Config.tracing.enabled,
})
rcaAgentServiceStack.addDependency(networkStack)
rcaAgentServiceStack.addDependency(eventBusStack)
rcaAgentServiceStack.addDependency(databaseStack)
rcaAgentServiceStack.addDependency(storageStack)

const tags = cdk.Tags.of(app)
tags.add('namespace', Config.app.ns)
tags.add('stage', Config.app.stage)

app.synth()
