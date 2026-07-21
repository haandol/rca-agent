import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { CcHeadlessStack } from '../lib/stacks/cc-headless-stack';

type CfnResource = {
  Properties?: Record<string, unknown>;
};

type IamStatement = {
  Action?: string | string[];
};

type PolicyDocument = {
  Statement?: IamStatement[];
};

type TaskRoleArn = {
  'Fn::GetAtt'?: [string, string];
};

type SynthesizedTemplates = {
  app: cdk.App;
  ccHeadlessStack: CcHeadlessStack;
  ccHeadless: Template;
  healthcare: Template;
};

function synthesize(): SynthesizedTemplates {
  const app = new cdk.App({ context: { ns: 'RcaAgentDev' } });
  const dependencies = new cdk.Stack(app, 'Dependencies');
  const vpc = new ec2.Vpc(dependencies, 'Vpc', { maxAzs: 2 });
  const alarmTopic = new sns.Topic(dependencies, 'AlarmTopic');
  const notificationTopic = new sns.Topic(dependencies, 'NotificationTopic');
  const sessionTable = new dynamodb.Table(dependencies, 'SessionTable', {
    partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  });
  const evidenceBucket = new s3.Bucket(dependencies, 'EvidenceBucket');

  const healthcareStack = new cdk.Stack(app, 'Healthcare');
  const healthcareCluster = new ecs.Cluster(
    healthcareStack,
    'HealthcareCluster',
    { vpc },
  );
  const healthcareTaskDefinition = new ecs.FargateTaskDefinition(
    healthcareStack,
    'HealthcareTaskDefinition',
  );
  healthcareTaskDefinition.addContainer('Healthcare', {
    image: ecs.ContainerImage.fromRegistry('placeholder'),
    portMappings: [{ containerPort: 8000 }],
  });
  const healthcareService = new ecs.FargateService(
    healthcareStack,
    'HealthcareService',
    {
      cluster: healthcareCluster,
      taskDefinition: healthcareTaskDefinition,
    },
  );

  new cdk.Stack(app, 'RemediationAgentStack');

  const stack = new CcHeadlessStack(app, 'CcHeadlessTest', {
    vpc,
    alarmTopic,
    notificationTopic,
    healthcareService,
    healthcareServiceHost: 'healthcare.rcaagentdev.local',
    rcaSessionTable: sessionTable,
    evidenceBucket,
    vectorBucketName: 'rca-test-vectors',
    reportBucket: 'rca-test-reports',
    imageTag: 'latest',
  });
  return {
    app,
    ccHeadlessStack: stack,
    ccHeadless: Template.fromStack(stack),
    healthcare: Template.fromStack(healthcareStack),
  };
}

function taskRoleStatements(template: Template): IamStatement[] {
  const taskDefinitions = Object.values(
    template.findResources('AWS::ECS::TaskDefinition'),
  ) as CfnResource[];
  expect(taskDefinitions).toHaveLength(1);

  const taskRoleArn = taskDefinitions[0].Properties?.TaskRoleArn as
    | TaskRoleArn
    | undefined;
  const taskRoleLogicalId = taskRoleArn?.['Fn::GetAtt']?.[0];
  expect(taskRoleLogicalId).toEqual(expect.any(String));

  const policies = Object.values(
    template.findResources('AWS::IAM::Policy'),
  ) as CfnResource[];
  return policies
    .filter((resource) => {
      const roles = (resource.Properties?.Roles ?? []) as {
        Ref?: string;
      }[];
      return roles.some((role) => role.Ref === taskRoleLogicalId);
    })
    .flatMap(
      (resource) =>
        (resource.Properties?.PolicyDocument as PolicyDocument | undefined)
          ?.Statement ?? [],
    );
}

test('CC task receives the Healthcare service host', () => {
  const { ccHeadless } = synthesize();

  ccHeadless.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'cc-headless',
        Environment: Match.arrayWith([
          {
            Name: 'HEALTHCARE_SERVICE_HOST',
            Value: 'healthcare.rcaagentdev.local',
          },
        ]),
      }),
    ]),
  });
});

test('CC queue visibility exceeds the analysis and staleness boundary', () => {
  const { ccHeadless } = synthesize();
  const queues = Object.values(
    ccHeadless.findResources('AWS::SQS::Queue'),
  ) as CfnResource[];
  const alarmQueue = queues.find(
    (resource) =>
      resource.Properties?.QueueName === 'RcaAgentDevCcHeadlessQueue',
  );
  const visibilityTimeout = alarmQueue?.Properties?.VisibilityTimeout;
  const analysisAndStalenessBoundarySeconds = 30 * 60;

  expect(visibilityTimeout).toBe(35 * 60);
  expect(visibilityTimeout as number).toBeGreaterThan(
    analysisAndStalenessBoundarySeconds,
  );
});

test('Healthcare allows CC ingress on port 8000 only', () => {
  const { healthcare } = synthesize();
  const ingressRules = Object.values(
    healthcare.findResources('AWS::EC2::SecurityGroupIngress'),
  ) as CfnResource[];

  expect(ingressRules).toHaveLength(1);
  expect(ingressRules[0].Properties).toEqual(
    expect.objectContaining({
      Description: 'CC Headless allowlisted Healthcare reset API calls',
      FromPort: 8000,
      IpProtocol: 'tcp',
      SourceSecurityGroupId: expect.objectContaining({
        'Fn::ImportValue': expect.any(String),
      }),
      ToPort: 8000,
    }),
  );
});

test('CC task role has no ECS service write permissions', () => {
  const { ccHeadless } = synthesize();
  const statements = taskRoleStatements(ccHeadless);
  const ecsActions = statements
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    )
    .filter(
      (action): action is string =>
        typeof action === 'string' && action.startsWith('ecs:'),
    );

  expect(ecsActions).toEqual([]);
});

test('CC stack has no dependency on the standalone remediation stack', () => {
  const { app, ccHeadlessStack } = synthesize();
  const assembly = app.synth();
  const ccArtifact = assembly.getStackArtifact(ccHeadlessStack.artifactId);

  expect(
    ccArtifact.dependencies.map((dependency) => dependency.id),
  ).not.toContain('RemediationAgentStack');
});
