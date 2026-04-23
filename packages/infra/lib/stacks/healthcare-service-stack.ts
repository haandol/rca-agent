import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as cloudmap from 'aws-cdk-lib/aws-servicediscovery';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly dbInstance: rds.DatabaseInstance;
  readonly alarmTopic: sns.ITopic;
  readonly imageTag: string;
  readonly tracing: boolean;
}

export class HealthcareServiceStack extends cdk.Stack {
  public readonly serviceName: string;
  public readonly clusterName: string;
  public readonly serviceHost: string;

  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;
    this.serviceName = `${ns}Healthcare`;
    this.clusterName = `${ns}Healthcare`;

    const namespace = new cloudmap.PrivateDnsNamespace(this, 'Namespace', {
      name: `${ns.toLowerCase()}.local`,
      vpc: props.vpc,
    });
    this.serviceHost = `healthcare.${ns.toLowerCase()}.local`;

    const cluster = this.newCluster(ns, props.vpc);
    const taskDefinition = this.newTaskDefinition(ns, props);
    const service = this.newService(ns, cluster, taskDefinition, props, namespace);
    this.newAlarms(ns, props, service);
  }

  private newCluster(ns: string, vpc: ec2.IVpc): ecs.Cluster {
    return new ecs.Cluster(this, 'Cluster', {
      clusterName: `${ns}Healthcare`,
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });
  }

  private newTaskDefinition(
    ns: string,
    props: IProps,
  ): ecs.FargateTaskDefinition {
    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      family: `${ns}Healthcare`,
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${ns}/healthcare`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const dbHost = props.dbInstance.instanceEndpoint.hostname;
    const dbPort = props.dbInstance.instanceEndpoint.port.toString();

    taskDef.addContainer('Healthcare', {
      containerName: 'healthcare',
      image: ecs.ContainerImage.fromRegistry(
        `${cdk.Aws.ACCOUNT_ID}.dkr.ecr.${cdk.Aws.REGION}.amazonaws.com/${ns.toLowerCase()}/healthcare:${props.imageTag}`,
      ),
      essential: true,
      environment: {
        AWS_REGION: cdk.Aws.REGION,
        DB_HOST: dbHost,
        DB_PORT: dbPort,
        DB_NAME: 'healthcare',
        OTEL_SERVICE_NAME: 'healthcare-sensor-app',
        FAULT_DB_LEAK: 'false',
        FAULT_SLOW_QUERY_MS: '0',
        FAULT_ERROR_RATE: '0.0',
      },
      secrets: {
        DB_USERNAME: ecs.Secret.fromSecretsManager(props.dbInstance.secret!, 'username'),
        DB_PASSWORD: ecs.Secret.fromSecretsManager(props.dbInstance.secret!, 'password'),
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'healthcare',
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

    if (props.tracing) {
      taskDef.addContainer('OtelCollector', {
        containerName: 'otel-collector',
        image: ecs.ContainerImage.fromRegistry(
          'public.ecr.aws/aws-observability/aws-otel-collector:latest',
        ),
        essential: false,
        logging: ecs.LogDrivers.awsLogs({
          streamPrefix: 'otel-collector',
          logGroup,
        }),
        portMappings: [{ containerPort: 4317 }, { containerPort: 4318 }],
      });
    }

    if (props.tracing) {
      taskDef.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: [
            'xray:PutTraceSegments',
            'xray:PutTelemetryRecords',
          ],
          resources: ['*'],
        }),
      );
    }

    this.grantEcrPull(taskDef);

    return taskDef;
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
    namespace: cloudmap.PrivateDnsNamespace,
  ): ecs.FargateService {
    const service = new ecs.FargateService(this, 'Service', {
      serviceName: `${ns}Healthcare`,
      cluster,
      taskDefinition,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      minHealthyPercent: 100,
      circuitBreaker: { enable: true, rollback: true },
      enableExecuteCommand: true,
      cloudMapOptions: {
        name: 'healthcare',
        cloudMapNamespace: namespace,
        dnsRecordType: cloudmap.DnsRecordType.A,
      },
    });

    return service;
  }

  private newAlarms(
    ns: string,
    props: IProps,
    service: ecs.FargateService,
  ): void {
    const alarmAction = new cw_actions.SnsAction(props.alarmTopic);

    const dbConnAlarm = new cloudwatch.Alarm(this, 'RdsHighConnections', {
      alarmName: `${ns}-Healthcare-RdsHighConnections`,
      metric: new cloudwatch.Metric({
        namespace: 'AWS/RDS',
        metricName: 'DatabaseConnections',
        dimensionsMap: {
          DBInstanceIdentifier: `${ns.toLowerCase()}-postgres`,
        },
        statistic: 'Maximum',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 30,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    dbConnAlarm.addAlarmAction(alarmAction);
    dbConnAlarm.addOkAction(alarmAction);

    const cpuAlarm = new cloudwatch.Alarm(this, 'EcsHighCPU', {
      alarmName: `${ns}-Healthcare-HighCPU`,
      metric: service.metricCpuUtilization({
        statistic: 'Average',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 80,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    cpuAlarm.addAlarmAction(alarmAction);
    cpuAlarm.addOkAction(alarmAction);

    const memAlarm = new cloudwatch.Alarm(this, 'EcsHighMemory', {
      alarmName: `${ns}-Healthcare-HighMemory`,
      metric: service.metricMemoryUtilization({
        statistic: 'Average',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 80,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    memAlarm.addAlarmAction(alarmAction);
    memAlarm.addOkAction(alarmAction);
  }
}
