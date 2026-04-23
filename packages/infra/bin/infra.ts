import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { EcrStack } from '../lib/stacks/ecr-stack';
import { NetworkStack } from '../lib/stacks/network-stack';
import { EventBusStack } from '../lib/stacks/event-bus-stack';
import { DatabaseStack } from '../lib/stacks/database-stack';
import { StorageStack } from '../lib/stacks/storage-stack';
import { RcaAgentServiceStack } from '../lib/stacks/rca-agent-service-stack';
import { RdsStack } from '../lib/stacks/rds-stack';
import { HealthcareServiceStack } from '../lib/stacks/healthcare-service-stack';
import { CcHeadlessStack } from '../lib/stacks/cc-headless-stack';
import { Config } from '../config/loader';

const app = new cdk.App({
  context: {
    ns: Config.app.ns,
    stage: Config.app.stage,
  },
});

const env = {
  region: Config.aws.region,
  account: process.env.CDK_DEFAULT_ACCOUNT,
};

const ecrStack = new EcrStack(app, `${Config.app.ns}EcrStack`, { env });

const networkStack = new NetworkStack(app, `${Config.app.ns}NetworkStack`, {
  env,
});

const eventBusStack = new EventBusStack(app, `${Config.app.ns}EventBusStack`, {
  env,
  notificationEmail: Config.alarm.notificationEmail,
});

const databaseStack = new DatabaseStack(app, `${Config.app.ns}DatabaseStack`, {
  env,
  rcaSessionTableName: Config.table.rcaSession.name,
});

const storageStack = new StorageStack(app, `${Config.app.ns}StorageStack`, {
  env,
  evidenceBucketName: Config.storage.evidenceBucket,
  vectorBucketName: Config.storage.vectorBucket,
});

const rdsStack = new RdsStack(app, `${Config.app.ns}RdsStack`, {
  env,
  vpc: networkStack.vpc,
});
rdsStack.addDependency(networkStack);

const healthcareServiceStack = new HealthcareServiceStack(
  app,
  `${Config.app.ns}HealthcareServiceStack`,
  {
    env,
    vpc: networkStack.vpc,
    dbInstance: rdsStack.instance,
    alarmTopic: eventBusStack.alarmTopic,
    imageTag: Config.healthcare.imageTag,
    tracing: Config.tracing.enabled,
  },
);
healthcareServiceStack.addDependency(ecrStack);
healthcareServiceStack.addDependency(networkStack);
healthcareServiceStack.addDependency(rdsStack);
healthcareServiceStack.addDependency(eventBusStack);

const rcaAgentServiceStack = new RcaAgentServiceStack(
  app,
  `${Config.app.ns}RcaAgentServiceStack`,
  {
    env,
    vpc: networkStack.vpc,
    alarmQueue: eventBusStack.alarmQueue,
    alarmTopic: eventBusStack.alarmTopic,
    rcaSessionTable: databaseStack.rcaSessionTable,
    evidenceBucket: storageStack.evidenceBucket,
    vectorBucketName: Config.storage.vectorBucket,
    imageTag: Config.agent.imageTag,
    tracing: Config.tracing.enabled,
  },
);
rcaAgentServiceStack.addDependency(ecrStack);
rcaAgentServiceStack.addDependency(networkStack);
rcaAgentServiceStack.addDependency(eventBusStack);
rcaAgentServiceStack.addDependency(databaseStack);
rcaAgentServiceStack.addDependency(storageStack);

const ccHeadlessStack = new CcHeadlessStack(
  app,
  `${Config.app.ns}CcHeadlessStack`,
  {
    env,
    vpc: networkStack.vpc,
    alarmTopic: eventBusStack.alarmTopic,
    notificationTopic: eventBusStack.alarmTopic,
    rcaSessionTable: databaseStack.rcaSessionTable,
    evidenceBucket: storageStack.evidenceBucket,
    vectorBucketName: Config.storage.vectorBucket,
    reportBucket: Config.storage.evidenceBucket,
    imageTag: Config.ccHeadless.imageTag,
  },
);
ccHeadlessStack.addDependency(ecrStack);
ccHeadlessStack.addDependency(networkStack);
ccHeadlessStack.addDependency(eventBusStack);
ccHeadlessStack.addDependency(databaseStack);
ccHeadlessStack.addDependency(storageStack);

const tags = cdk.Tags.of(app);
tags.add('namespace', Config.app.ns);
tags.add('stage', Config.app.stage);

app.synth();
