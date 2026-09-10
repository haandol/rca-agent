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
import { grantEcrPull } from '../constructs/ecr-access';
import {
  healthcareExecutionRoleName,
  healthcareTaskRoleName,
} from '../constructs/healthcare-role-names';

interface IProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly dbInstance: rds.DatabaseInstance;
  readonly alarmTopic: sns.ITopic;
  readonly imageTag: string;
  /** Optional immutable image pin; imageTag still supplies the revision label. */
  readonly imageDigest?: string;
  readonly tracing: boolean;
  readonly queryLatencyThresholdMs?: number;
  readonly controlledEnvironment?: Readonly<Record<string, string | undefined>>;
}

export class HealthcareServiceStack extends cdk.Stack {
  public readonly serviceName: string;
  public readonly clusterName: string;
  public readonly serviceHost: string;
  public readonly service: ecs.FargateService;

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
    const service = this.newService(
      ns,
      cluster,
      taskDefinition,
      props,
      namespace,
    );
    this.service = service;
    this.newAlarms(ns, props, service);
  }

  private newCluster(ns: string, vpc: ec2.IVpc): ecs.Cluster {
    return new ecs.Cluster(this, 'Cluster', {
      clusterName: `${ns}Healthcare`,
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });
  }

  /** Create the private service, optionally pinning its image without changing its revision label. */
  private newTaskDefinition(
    ns: string,
    props: IProps,
  ): ecs.FargateTaskDefinition {
    if (
      props.imageDigest !== undefined &&
      (props.imageDigest.length !== 71 ||
        !/^sha256:[a-f0-9]{64}$/.test(props.imageDigest))
    ) {
      throw new Error(
        'healthcare.imageDigest must be sha256:<64 lowercase hex digits>',
      );
    }
    const imageSuffix =
      props.imageDigest === undefined
        ? `:${props.imageTag}`
        : `@${props.imageDigest}`;

    const taskRole = new iam.Role(this, 'TaskRole', {
      roleName: healthcareTaskRoleName(ns),
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });
    const executionRole = new iam.Role(this, 'ExecutionRole', {
      roleName: healthcareExecutionRoleName(ns),
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      family: `${ns}Healthcare`,
      cpu: 512,
      memoryLimitMiB: 1024,
      taskRole,
      executionRole,
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
        `${cdk.Aws.ACCOUNT_ID}.dkr.ecr.${cdk.Aws.REGION}.amazonaws.com/${ns.toLowerCase()}/healthcare${imageSuffix}`,
      ),
      essential: true,
      environment: {
        AWS_REGION: cdk.Aws.REGION,
        DB_HOST: dbHost,
        DB_PORT: dbPort,
        DB_NAME: 'healthcare',
        OTEL_SERVICE_NAME: 'healthcare-sensor-app',
        DEPLOYED_REVISION: props.imageTag,
        // Stated here rather than left to the app's defaults because the
        // connection alarm threshold is derived from this capacity: the leak has
        // to cross the threshold before it exhausts the pool.
        DB_POOL_SIZE: '5',
        DB_MAX_OVERFLOW: '10',
        FAULT_DB_LEAK: 'false',
        FAULT_SLOW_QUERY_MS: '0',
        FAULT_ERROR_RATE: '0.0',
        ...this.controlledEnvironment(props.controlledEnvironment ?? {}),
      },
      secrets: {
        DB_USERNAME: ecs.Secret.fromSecretsManager(
          props.dbInstance.secret!,
          'username',
        ),
        DB_PASSWORD: ecs.Secret.fromSecretsManager(
          props.dbInstance.secret!,
          'password',
        ),
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
          actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'],
          resources: ['*'],
        }),
      );
    }

    grantEcrPull(taskDef);

    return taskDef;
  }

  /** Validate only approved workload controls; fault settings are never defaults. */
  private controlledEnvironment(
    values: Readonly<Record<string, string | undefined>>,
  ): Record<string, string> {
    const booleans = ['TRAFFIC_ENABLED', 'DB_OBSERVABILITY_ENABLED'];
    const positive = [
      'TRAFFIC_INTERVAL_SECONDS',
      'TRAFFIC_MAX_CONCURRENCY',
      'TRAFFIC_QUERY_LIMIT',
      'DB_POOL_TIMEOUT_SECONDS',
      'DB_OBSERVABILITY_INTERVAL_SECONDS',
    ];
    const integers = [
      'TRAFFIC_MAX_CONCURRENCY',
      'TRAFFIC_QUERY_LIMIT',
      'TRAFFIC_SEED',
      'DB_STATEMENT_TIMEOUT_MS',
    ];
    const allowed = new Set([
      ...booleans,
      ...positive,
      ...integers,
      'TRAFFIC_PATIENT_ID',
    ]);
    const result: Record<string, string> = {};
    for (const [key, value] of Object.entries(values)) {
      if (value === undefined) continue;
      if (!allowed.has(key))
        throw new Error(`Unsupported healthcare setting: ${key}`);
      if (booleans.includes(key) && !['true', 'false'].includes(value)) {
        throw new Error(`${key} must be true or false`);
      }
      if (positive.includes(key) || integers.includes(key)) {
        const number = Number(value);
        if (
          value.trim() === '' ||
          !Number.isFinite(number) ||
          (positive.includes(key) && number <= 0) ||
          (integers.includes(key) && !Number.isSafeInteger(number)) ||
          (key === 'DB_STATEMENT_TIMEOUT_MS' && number < 0)
        ) {
          throw new Error(`Invalid healthcare setting: ${key}`);
        }
      }
      result[key] = value;
    }
    return result;
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

  /** Publish user-visible symptoms while retaining cause-level evidence alarms. */
  private newAlarms(
    ns: string,
    props: IProps,
    service: ecs.FargateService,
  ): void {
    const alarmAction = new cw_actions.SnsAction(props.alarmTopic);
    const resourceCoordinates =
      `Resources: LogGroup=/ecs/${ns}/healthcare; ` +
      `ECSCluster=${this.clusterName}; ECSService=${this.serviceName}; ` +
      `RDSInstance=${ns.toLowerCase()}-postgres.`;

    // RCA entry point. The app emits this via EMF, so the alarm reports a
    // domain symptom without naming which subsystem caused it. The cause-level
    // alarms below stay in place as evidence the agent has to find on its own.
    const ingestFailureAlarm = new cloudwatch.Alarm(
      this,
      'VitalIngestFailures',
      {
        alarmName: `${ns}-Healthcare-VitalIngestFailures`,
        alarmDescription:
          'Patient vital readings are failing to be recorded. Impact: vitals are missing from the record and abnormal-value alerts are not raised. ' +
          resourceCoordinates,
        metric: new cloudwatch.Metric({
          namespace: 'Healthcare/Sensor',
          metricName: 'VitalIngestFailures',
          dimensionsMap: { ServiceName: 'healthcare-sensor-app' },
          statistic: 'Sum',
          period: cdk.Duration.minutes(1),
        }),
        threshold: 1,
        evaluationPeriods: 2,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      },
    );
    ingestFailureAlarm.addAlarmAction(alarmAction);
    ingestFailureAlarm.addOkAction(alarmAction);

    const queryLatencyAlarm = new cloudwatch.Alarm(
      this,
      'PatientVitalsQueryLatency',
      {
        alarmName: `${ns}-Healthcare-PatientVitalsQueryLatency`,
        alarmDescription:
          'Patient vital record queries are slow. Initial threshold requires calibration against healthy and restored traffic. ' +
          resourceCoordinates,
        metric: new cloudwatch.Metric({
          namespace: 'Healthcare/Sensor',
          metricName: 'PatientVitalsQueryDuration',
          dimensionsMap: { ServiceName: 'healthcare-sensor-app' },
          unit: cloudwatch.Unit.MILLISECONDS,
          statistic: 'Average',
          period: cdk.Duration.minutes(1),
        }),
        threshold: props.queryLatencyThresholdMs ?? 500,
        evaluationPeriods: 2,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.MISSING,
      },
    );
    queryLatencyAlarm.addAlarmAction(alarmAction);
    queryLatencyAlarm.addOkAction(alarmAction);

    // Threshold sits between normal usage and the app's pool capacity, so a leak
    // trips this alarm before it exhausts the pool and turns into the ingest
    // failures the symptom alarm watches. Above the pool ceiling the leak would
    // starve requests while this metric stayed quiet, leaving the cause-level
    // evidence the agent is supposed to find absent from the timeline.
    new cloudwatch.Alarm(this, 'RdsHighConnections', {
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
      threshold: 12,
      evaluationPeriods: 2,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new cloudwatch.Alarm(this, 'EcsHighCPU', {
      alarmName: `${ns}-Healthcare-HighCPU`,
      metric: service.metricCpuUtilization({
        statistic: 'Average',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 80,
      evaluationPeriods: 2,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new cloudwatch.Alarm(this, 'EcsHighMemory', {
      alarmName: `${ns}-Healthcare-HighMemory`,
      metric: service.metricMemoryUtilization({
        statistic: 'Average',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 80,
      evaluationPeriods: 2,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}
