import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly notificationTopic: sns.ITopic;
  readonly healthcareService: ecs.FargateService;
  readonly healthcareServiceHost: string;
  readonly healthcareClusterName: string;
  readonly healthcareServiceName: string;
  readonly imageTag: string;
  // desiredCount 0 이면 파이프라인이 알림만 발행하고 복구는 실행되지 않는다
  // (ADR agent/0012의 점진적 활성화 — 피처 플래그를 desired count로 제어).
  readonly desiredCount: number;
}

const HEALTHCARE_PORT = 8000;

export class RemediationAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;

    const deadLetterQueue = this.newDeadLetterQueue(ns);
    const queue = this.newRemediationQueue(ns, props.notificationTopic, deadLetterQueue);
    const cluster = this.newCluster(ns, props.vpc);
    const taskDefinition = this.newTaskDefinition(ns, props, queue);
    const service = this.newService(ns, cluster, taskDefinition, props);

    // 복구 에이전트가 Healthcare 서비스의 리셋 API를 호출할 수 있도록 인그레스 허용
    props.healthcareService.connections.allowFrom(
      service,
      ec2.Port.tcp(HEALTHCARE_PORT),
      'Remediation agent fault-reset API calls',
    );
  }

  private newDeadLetterQueue(ns: string): sqs.Queue {
    return new sqs.Queue(this, 'DeadLetterQueue', {
      queueName: `${ns}RemediationDLQ`,
      visibilityTimeout: cdk.Duration.minutes(10),
      retentionPeriod: cdk.Duration.days(14),
    });
  }

  private newRemediationQueue(
    ns: string,
    notificationTopic: sns.ITopic,
    deadLetterQueue: sqs.Queue,
  ): sqs.Queue {
    const queue = new sqs.Queue(this, 'RemediationQueue', {
      queueName: `${ns}RemediationQueue`,
      visibilityTimeout: cdk.Duration.minutes(15),
      retentionPeriod: cdk.Duration.days(4),
      deadLetterQueue: {
        queue: deadLetterQueue,
        maxReceiveCount: 3,
      },
    });

    // RCA 완료 이벤트만 구독 — 복구 결과 알림(remediation_complete)이 되돌아와
    // 무한 루프가 되는 것을 메시지 속성 필터로 차단한다 (ADR agent/0012).
    notificationTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(queue, {
        rawMessageDelivery: false,
        filterPolicy: {
          event_type: sns.SubscriptionFilter.stringFilter({
            allowlist: ['rca_complete'],
          }),
        },
      }),
    );

    return queue;
  }

  private newCluster(ns: string, vpc: ec2.IVpc): ecs.Cluster {
    return new ecs.Cluster(this, 'Cluster', {
      clusterName: `${ns}Remediation`,
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });
  }

  private newTaskDefinition(
    ns: string,
    props: IProps,
    queue: sqs.Queue,
  ): ecs.FargateTaskDefinition {
    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      family: `${ns}Remediation`,
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${ns}/remediation`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    taskDef.addContainer('Remediation', {
      containerName: 'remediation',
      image: ecs.ContainerImage.fromRegistry(
        `${cdk.Aws.ACCOUNT_ID}.dkr.ecr.${cdk.Aws.REGION}.amazonaws.com/${ns.toLowerCase()}/rca-agent:${props.imageTag}`,
      ),
      command: ['python', '-m', 'rca_agent.remediation_main'],
      essential: true,
      stopTimeout: cdk.Duration.seconds(120),
      environment: {
        AWS_REGION: cdk.Aws.REGION,
        REMEDIATION_QUEUE_URL: queue.queueUrl,
        SNS_NOTIFICATION_TOPIC_ARN: props.notificationTopic.topicArn,
        HEALTHCARE_SERVICE_HOST: props.healthcareServiceHost,
        ECS_CLUSTER_NAME: props.healthcareClusterName,
        ECS_SERVICE_NAME: props.healthcareServiceName,
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'remediation',
        logGroup,
      }),
      healthCheck: {
        command: [
          'CMD-SHELL',
          'python -c "import urllib.request; urllib.request.urlopen(\'http://localhost:8000/healthz\')" || exit 1',
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        startPeriod: cdk.Duration.seconds(30),
        retries: 3,
      },
      portMappings: [{ containerPort: 8000 }],
    });

    this.grantTaskPermissions(taskDef, props, queue);
    this.grantEcrPull(taskDef);

    return taskDef;
  }

  private grantTaskPermissions(
    taskDef: ecs.FargateTaskDefinition,
    props: IProps,
    queue: sqs.Queue,
  ): void {
    queue.grantConsumeMessages(taskDef.taskRole);

    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
        ],
        resources: ['*'],
      }),
    );

    // 복구 후 검증 — CloudWatch 메트릭 조회
    taskDef.taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchReadOnlyAccess'),
    );

    // 복구 결과 알림 발행
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['sns:Publish'],
        resources: [props.notificationTopic.topicArn],
      }),
    );

    // ECS 강제 새 배포(롤백) 및 상태 조회 — 복구 액션
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['ecs:UpdateService', 'ecs:DescribeServices'],
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
    props: IProps,
  ): ecs.FargateService {
    return new ecs.FargateService(this, 'Service', {
      serviceName: `${ns}Remediation`,
      cluster,
      taskDefinition,
      desiredCount: props.desiredCount,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      minHealthyPercent: 100,
      circuitBreaker: { enable: true, rollback: true },
      enableExecuteCommand: true,
    });
  }
}
