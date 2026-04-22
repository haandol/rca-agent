import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly dbInstance: rds.DatabaseInstance;
  readonly imageTag: string;
  readonly tracing: boolean;
}

export class HealthcareServiceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;

    const cluster = this.newCluster(ns, props.vpc);
    const taskDefinition = this.newTaskDefinition(ns, props);
    this.newService(ns, cluster, taskDefinition, props);
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
    });

    return service;
  }
}
