// Synthetic approval contract, checked against the actual Python runtime by the harness.
export function deploymentBook() {
  const prefix = 'arn:aws:ecs:us-east-1:123456789012:';
  const scope = {
    account_id: '123456789012',
    region: 'us-east-1',
    cluster: prefix + 'cluster/cluster',
    service: prefix + 'service/cluster/app',
    container_name: 'app',
    desired_count: 1,
  };
  const normal = {
    task_definition_arn: prefix + 'task-definition/app:1',
    image_digest: 'sha256:' + 'a'.repeat(64),
  };
  const current = {
    task_definition_arn: prefix + 'task-definition/app:2',
    image_digest: 'sha256:' + 'b'.repeat(64),
    deployment_id: 'ecs-svc/fault',
  };
  const service_settings = {
    deploymentConfiguration: {},
    networkConfiguration: {},
    capacityProviderStrategy: [],
    launchType: 'FARGATE',
    platformVersion: 'LATEST',
    schedulingStrategy: 'REPLICA',
  };
  const metric = (name) => ({
    namespace: 'Healthcare/Sensor',
    metric_name: name,
    dimensions: { ServiceName: 'logical-writer' },
  });
  return {
    playbook_id: 'fixed-book',
    rollback_context: {
      baseline_ref: {
        bucket: 'evidence',
        key: 'normal/run.json',
        sha256: 'c'.repeat(64),
      },
      scope: {
        account_id: scope.account_id,
        region: scope.region,
        cluster_arn: scope.cluster,
        service_arn: scope.service,
        service_name: 'app',
        container_name: 'app',
        desired_count: 1,
        log_group: '/ecs/app',
      },
      normal,
      current,
      service_settings,
      write_accounting: {
        namespace: 'Healthcare/Sensor',
        dimensions: { ServiceName: 'logical-writer' },
        attempts_metric: 'Attempts',
        failures_metric: 'Failures',
        operation_kind: 'write',
        accounting: 'completed',
        source_ref: 'cloudwatch-logs:///ecs/app/ecs/app/normal-task#event-1',
        ...normal,
      },
    },
    execution_steps: [
      {
        step_id: 'discover',
        action: 'Read metric coordinates',
        success_criteria: 'observed coordinates and alarm',
        commands: [
          'aws cloudwatch list-metrics --namespace Healthcare/Sensor --region us-east-1',
          'aws cloudwatch describe-alarms --alarm-names IngestFailures --region us-east-1',
        ],
      },
      {
        step_id: 'rollback',
        action: 'Restore verified normal revision',
        success_criteria: 'rollback acknowledged',
        commands: [
          `aws ecs update-service --cluster ${scope.cluster} --service ${scope.service} --task-definition ${normal.task_definition_arn} --region us-east-1`,
        ],
        ecs_service_precondition: {
          ...scope,
          expected_task_definition: current.task_definition_arn,
          expected_image_digest: current.image_digest,
          expected_deployment_id: current.deployment_id,
          service_settings,
        },
      },
      {
        step_id: 'converge',
        action: 'Observe deployment convergence',
        success_criteria:
          'all normal app tasks healthy and fault tasks stopped',
        deployment_wait: {
          ...scope,
          action_step_id: 'rollback',
          task_definition: normal.task_definition_arn,
          image_digest: normal.image_digest,
          max_wait_seconds: 900,
        },
      },
      {
        step_id: 'metrics',
        action: 'Observe completed writes',
        success_criteria:
          'Failures zero with attempts and IngestFailures OK; completed writes proven',
        metric_wait: {
          deployment_step_id: 'converge',
          metrics: {
            attempts: metric('Attempts'),
            failures: metric('Failures'),
          },
          failure_alarm_name: 'IngestFailures',
          region: 'us-east-1',
          completed_work_evidence: {
            record_index: 'approved_context',
            json_pointer: '/playbook/rollback_context/write_accounting',
          },
        },
      },
    ],
  };
}

export function ecsObservations(book = deploymentBook()) {
  const { scope, normal, current, service_settings } = book.rollback_context;
  const taskArn = `arn:aws:ecs:${scope.region}:${scope.account_id}:task/cluster/fault`;
  return {
    target: {
      taskDefinition: {
        taskDefinitionArn: normal.task_definition_arn,
        status: 'ACTIVE',
        containerDefinitions: [
          {
            name: scope.container_name,
            image: 'repository/app@' + normal.image_digest,
          },
        ],
      },
    },
    running: { taskArns: [taskArn] },
    pending: { taskArns: [] },
    tasks: {
      tasks: [
        {
          taskArn,
          clusterArn: scope.cluster_arn,
          group: 'service:' + scope.service_name,
          taskDefinitionArn: current.task_definition_arn,
          lastStatus: 'RUNNING',
          desiredStatus: 'RUNNING',
          healthStatus: 'HEALTHY',
          containers: [
            {
              name: scope.container_name,
              lastStatus: 'RUNNING',
              healthStatus: 'HEALTHY',
              imageDigest: current.image_digest,
            },
          ],
        },
      ],
    },
    service: {
      services: [
        {
          serviceArn: scope.service_arn,
          clusterArn: scope.cluster_arn,
          status: 'ACTIVE',
          desiredCount: scope.desired_count,
          runningCount: scope.desired_count,
          pendingCount: 0,
          taskDefinition: current.task_definition_arn,
          deploymentController: { type: 'ECS' },
          deployments: [
            {
              id: current.deployment_id,
              taskDefinition: current.task_definition_arn,
              status: 'PRIMARY',
              rolloutState: 'COMPLETED',
            },
          ],
          ...service_settings,
        },
      ],
    },
  };
}

export function deploymentCases() {
  const cases = [
    {
      name: 'complete causal recovery and logical metric ServiceName',
      book: deploymentBook(),
      valid: true,
    },
  ];
  const withoutAccounting = deploymentBook();
  delete withoutAccounting.rollback_context.write_accounting;
  delete withoutAccounting.execution_steps[3].metric_wait
    .completed_work_evidence;
  cases.push({
    name: 'optional write accounting absent',
    book: withoutAccounting,
    valid: true,
  });
  for (const seconds of [1, 300, 301, 900]) {
    const book = deploymentBook();
    book.execution_steps[2].deployment_wait.max_wait_seconds = seconds;
    book.execution_steps[3].metric_wait.max_wait_seconds = seconds;
    cases.push({ name: `both wait bounds ${seconds}`, book, valid: true });
  }
  const add = (name, mutate) => {
    const book = deploymentBook();
    mutate(book);
    cases.push({ name, book, valid: false });
  };
  for (const seconds of [0, 901, null, true, 1.5, '900']) {
    add(`deployment duration rejects ${JSON.stringify(seconds)}`, (b) => {
      b.execution_steps[2].deployment_wait.max_wait_seconds = seconds;
    });
    add(`metric duration rejects ${JSON.stringify(seconds)}`, (b) => {
      b.execution_steps[3].metric_wait.max_wait_seconds = seconds;
    });
  }
  add('deployment duration must remain explicitly recorded', (b) => {
    delete b.execution_steps[2].deployment_wait.max_wait_seconds;
  });
  add('model guard stripped', (b) => {
    delete b.execution_steps[1].ecs_service_precondition;
  });
  add('model stale deployment ID', (b) => {
    b.execution_steps[1].ecs_service_precondition.expected_deployment_id =
      'ecs-svc/stale';
  });
  add('different normal context', (b) => {
    b.rollback_context.normal.image_digest = 'sha256:' + 'd'.repeat(64);
  });
  add('different current context', (b) => {
    b.rollback_context.current.task_definition_arn += '0';
  });
  add('missing post convergence metrics', (b) => {
    b.execution_steps.pop();
  });
  add('missing convergence', (b) => {
    b.execution_steps.splice(2, 1);
  });
  add('metrics references rollback instead of convergence', (b) => {
    b.execution_steps[3].metric_wait.deployment_step_id = 'rollback';
  });
  add('metrics references foreign convergence', (b) => {
    b.execution_steps[3].metric_wait.deployment_step_id = 'foreign';
  });
  add('earlier metrics cannot substitute', (b) => {
    const step = b.execution_steps.pop();
    b.execution_steps.splice(2, 0, step);
  });
  add('stop task metrics cannot substitute', (b) => {
    b.execution_steps[0].commands.push(
      'aws ecs stop-task --cluster cluster --task arn:aws:ecs:us-east-1:123456789012:task/cluster/owner --region us-east-1',
    );
    delete b.execution_steps[3].metric_wait.deployment_step_id;
    b.execution_steps[3].metric_wait.action_step_id = 'discover';
  });
  add('two metric anchors', (b) => {
    b.execution_steps[3].metric_wait.action_step_id = 'rollback';
  });
  add('different metric region', (b) => {
    b.execution_steps[3].metric_wait.region = 'us-west-2';
  });
  add('duplicate convergence', (b) => {
    const step = structuredClone(b.execution_steps[2]);
    step.step_id = 'duplicate';
    b.execution_steps.splice(3, 0, step);
  });
  add('mixed operation', (b) => {
    b.execution_steps[2].commands = b.execution_steps[0].commands;
  });
  add('different account guard', (b) => {
    b.execution_steps[1].ecs_service_precondition.account_id = '999999999999';
  });
  add('extra service setting', (b) => {
    b.execution_steps[1].ecs_service_precondition.service_settings.unknown = true;
  });
  add('missing deployment ID', (b) => {
    delete b.execution_steps[1].ecs_service_precondition.expected_deployment_id;
  });
  add('wrong write proof image', (b) => {
    b.rollback_context.write_accounting.image_digest =
      'sha256:' + 'd'.repeat(64);
  });
  add('wrong write log scope', (b) => {
    b.rollback_context.write_accounting.source_ref =
      'cloudwatch-logs:///other/ecs/app/task#id';
  });
  add('unsupported context field', (b) => {
    b.rollback_context.hidden_normal = true;
  });
  return cases;
}
