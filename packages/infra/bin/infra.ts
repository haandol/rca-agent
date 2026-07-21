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
import { RemediationAgentStack } from '../lib/stacks/remediation-agent-stack';
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
    healthcareService: healthcareServiceStack.service,
    healthcareServiceHost: healthcareServiceStack.serviceHost,
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
// Healthcare owns the ingress rule and therefore references the CC security
// group. The host is a plain DNS string, so no reverse dependency is created.

const remediationAgentStack = new RemediationAgentStack(
  app,
  `${Config.app.ns}RemediationAgentStack`,
  {
    env,
    vpc: networkStack.vpc,
    notificationTopic: eventBusStack.alarmTopic,
    healthcareService: healthcareServiceStack.service,
    healthcareServiceHost: healthcareServiceStack.serviceHost,
    rcaSessionTable: databaseStack.rcaSessionTable,
    imageTag: Config.remediation.imageTag,
    desiredCount: Config.remediation.desiredCount,
  },
);
remediationAgentStack.addDependency(ecrStack);
remediationAgentStack.addDependency(networkStack);
remediationAgentStack.addDependency(eventBusStack);
remediationAgentStack.addDependency(databaseStack);
// NOTE: healthcareServiceStack에 명시적 의존을 두지 않는다. 복구 에이전트가
// Healthcare 서비스의 SG 인그레스를 여는 순간 Healthcare 스택이 Remediation
// 스택을 참조하게 되어, 반대 방향 의존을 추가하면 순환 참조가 된다.
// serviceHost는 일반 DNS 문자열이라 CFN 교차 참조를 만들지 않는다.

const tags = cdk.Tags.of(app);
tags.add('namespace', Config.app.ns);
tags.add('stage', Config.app.stage);

app.synth();
