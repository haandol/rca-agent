import {
  DescribeServicesCommand,
  DescribeTaskDefinitionCommand,
  DescribeTasksCommand,
  ListTasksCommand,
  type ECSClient,
  type Task,
} from '@aws-sdk/client-ecs';
import {
  deploymentRecord,
  sameDeploymentValue,
  serviceSettingDefaults,
} from './deploymentContract.ts';

type DataRecord = Record<string, unknown>;
function requireObservation(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Error(message);
}
/** Match runtime read_json: incomplete/paginated/error payloads cannot prove safety. */
function requireCompleteResponse(value: unknown): void {
  const result = deploymentRecord(value);
  requireObservation(
    !result.nextToken &&
      !(Array.isArray(result.failures)
        ? result.failures.length
        : result.failures),
    'ECS 조회 응답이 불완전하거나 실패했습니다.',
  );
}
/** Check the actual healthy target, fault task population, and service last, in the
 * same order as service_deployment.check_precondition. Reads are bounded and fail
 * closed on errors, pagination, incomplete responses, or drift. No writes occur.
 * Caller must validate the stored contract and expected digest before invoking.
 */
export async function verifyDeploymentApproval(
  playbook: DataRecord,
  clientForRegion: (region: string) => Pick<ECSClient, 'send'>,
): Promise<void> {
  const steps = playbook.execution_steps as DataRecord[];
  for (const step of steps) {
    if (!step.deployment_wait) continue;
    const wait = deploymentRecord(step.deployment_wait);
    const action = steps.find((s) => s.step_id === wait.action_step_id)!;
    const guard = deploymentRecord(action.ecs_service_precondition);
    const client = clientForRegion(String(guard.region));
    const options = { abortSignal: AbortSignal.timeout(30_000) };
    const target = await client.send(
      new DescribeTaskDefinitionCommand({
        taskDefinition: String(wait.task_definition),
      }),
      options,
    );
    requireCompleteResponse(target);
    const definition = target.taskDefinition;
    const apps =
      definition?.containerDefinitions?.filter(
        (c) => c.name === wait.container_name,
      ) ?? [];
    requireObservation(
      definition?.taskDefinitionArn === wait.task_definition &&
        definition?.status === 'ACTIVE' &&
        apps.length === 1 &&
        apps[0]?.image?.endsWith('@' + wait.image_digest),
      '정상 태스크 정의 또는 앱 이미지 지문을 확인할 수 없습니다.',
    );
    const arns: string[] = [];
    for (const desiredStatus of ['RUNNING', 'PENDING'] as const) {
      const result = await client.send(
        new ListTasksCommand({
          cluster: String(guard.cluster),
          serviceName: String(guard.service).split('/').at(-1),
          desiredStatus,
        }),
        options,
      );
      requireCompleteResponse(result);
      requireObservation(
        !result.nextToken &&
          Array.isArray(result.taskArns) &&
          new Set(result.taskArns).size === result.taskArns.length,
        '서비스 태스크 목록이 불완전합니다.',
      );
      arns.push(...result.taskArns);
    }
    const unique = [...new Set(arns)];
    const tasks: Task[] = [];
    for (let i = 0; i < unique.length; i += 100) {
      const batch = unique.slice(i, i + 100);
      const result = await client.send(
        new DescribeTasksCommand({
          cluster: String(guard.cluster),
          tasks: batch,
        }),
        options,
      );
      requireCompleteResponse(result);
      const rows = result.tasks ?? [];
      requireObservation(
        !result.failures?.length &&
          rows.length === batch.length &&
          new Set(rows.map((t) => t.taskArn)).size === batch.length &&
          rows.every((t) => t.taskArn && batch.includes(t.taskArn)),
        '태스크 조회에 실패했거나 중복·누락이 있습니다.',
      );
      tasks.push(...rows);
    }
    // Match runtime: re-read service after target and task reads to catch drift.
    const result = await client.send(
      new DescribeServicesCommand({
        cluster: String(guard.cluster),
        services: [String(guard.service)],
      }),
      options,
    );
    requireCompleteResponse(result);
    requireObservation(
      !result.failures?.length && result.services?.length === 1,
      '승인 대상 서비스를 정확히 조회할 수 없습니다.',
    );
    const service = result.services[0]!;
    requireObservation(
      service.serviceArn === guard.service &&
        service.clusterArn === guard.cluster &&
        service.status === 'ACTIVE' &&
        service.desiredCount === guard.desired_count &&
        (service.deploymentController?.type ?? 'ECS') === 'ECS',
      '서비스 범위·상태·태스크 수 또는 배포 방식이 변경되었습니다.',
    );
    const settings = Object.fromEntries(
      Object.entries(serviceSettingDefaults).map(([key, value]) => [
        key,
        (service as DataRecord)[key] === undefined
          ? value
          : (service as DataRecord)[key],
      ]),
    );
    requireObservation(
      sameDeploymentValue(settings, guard.service_settings),
      '서비스 설정이 변경되었습니다. 새 분석과 검토가 필요합니다.',
    );
    const deployments = service.deployments ?? [],
      current = deployments[0];
    requireObservation(
      service.taskDefinition === guard.expected_task_definition &&
        deployments.length === 1 &&
        current?.taskDefinition === guard.expected_task_definition &&
        current?.status === 'PRIMARY' &&
        current?.rolloutState === 'COMPLETED' &&
        current?.id === guard.expected_deployment_id,
      '현재 배포 ID 또는 결함 태스크 정의가 변경되었습니다. 새 검토가 필요합니다.',
    );
    requireObservation(
      service.pendingCount === 0 &&
        service.runningCount === guard.desired_count &&
        tasks.length === guard.desired_count,
      '결함 서비스의 태스크 구성이 변경되었습니다.',
    );
    for (const task of tasks) {
      const containers = task.containers ?? [],
        app = containers.filter((c) => c.name === guard.container_name);
      requireObservation(
        task.clusterArn === guard.cluster &&
          task.group === 'service:' + String(guard.service).split('/').at(-1) &&
          new Set(containers.map((c) => c.name)).size === containers.length &&
          task.taskDefinitionArn === guard.expected_task_definition &&
          task.lastStatus === 'RUNNING' &&
          task.desiredStatus === 'RUNNING' &&
          task.healthStatus === 'HEALTHY' &&
          app.length === 1 &&
          app[0]?.lastStatus === 'RUNNING' &&
          app[0]?.healthStatus === 'HEALTHY' &&
          app[0]?.imageDigest === guard.expected_image_digest,
        '결함 앱 태스크의 소속·정의·이미지·정상 상태가 변경되었습니다.',
      );
    }
  }
}
