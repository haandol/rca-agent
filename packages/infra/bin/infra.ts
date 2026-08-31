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
import { HeadlessCodexStack } from '../lib/stacks/headless-codex-stack';
import { PlaybookExecutionStack } from '../lib/stacks/playbook-execution-stack';
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
    notificationTopic: eventBusStack.notificationTopic,
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

// 분석은 읽기 전용이다. Healthcare 서비스로의 접근 경로를 두지 않는다.
const headlessCodexStack = new HeadlessCodexStack(
  app,
  // Keep the deployed stack name while replacing the runtime in place.
  `${Config.app.ns}CcHeadlessStack`,
  {
    env,
    vpc: networkStack.vpc,
    alarmTopic: eventBusStack.alarmTopic,
    notificationTopic: eventBusStack.notificationTopic,
    rcaSessionTable: databaseStack.rcaSessionTable,
    evidenceBucket: storageStack.evidenceBucket,
    vectorBucketName: Config.storage.vectorBucket,
    reportBucket: Config.storage.evidenceBucket,
    imageTag: Config.headlessCodex.imageTag,
  },
);
headlessCodexStack.addDependency(ecrStack);
headlessCodexStack.addDependency(networkStack);
headlessCodexStack.addDependency(eventBusStack);
headlessCodexStack.addDependency(databaseStack);
headlessCodexStack.addDependency(storageStack);

// 실행은 사용자가 승인 요청을 발행할 때만 시작된다. 이벤트 구독을 두지 않으므로
// 승인 없이 실행이 기동될 경로가 인프라에 존재하지 않는다.
const playbookExecutionStack = new PlaybookExecutionStack(
  app,
  `${Config.app.ns}PlaybookExecutionStack`,
  {
    env,
    vpc: networkStack.vpc,
    healthcareService: healthcareServiceStack.service,
    rcaSessionTable: databaseStack.rcaSessionTable,
    evidenceBucket: storageStack.evidenceBucket,
    vectorBucketName: Config.storage.vectorBucket,
    imageTag: Config.execution.imageTag,
  },
);
playbookExecutionStack.addDependency(ecrStack);
playbookExecutionStack.addDependency(networkStack);
playbookExecutionStack.addDependency(databaseStack);
playbookExecutionStack.addDependency(storageStack);
// NOTE: healthcareServiceStack에 명시적 의존을 두지 않는다. 실행 에이전트가
// Healthcare 서비스의 SG 인그레스를 여는 순간 Healthcare 스택이 실행 스택을
// 참조하게 되어, 반대 방향 의존을 추가하면 순환 참조가 된다.

const tags = cdk.Tags.of(app);
tags.add('namespace', Config.app.ns);
tags.add('stage', Config.app.stage);

app.synth();
