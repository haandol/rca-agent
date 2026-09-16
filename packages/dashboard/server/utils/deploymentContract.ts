/** Exact approval contract mirrored from headless-codex runbook_contract.py and
 * service_deployment.validate_rollback_context. Never repair model-owned fields.
 * The caller supplies the current stored playbook, never a request-body context.
 */
type RecordValue = Record<string, unknown>;
export const deploymentScopeFields = [
  'account_id',
  'region',
  'cluster',
  'service',
  'container_name',
  'desired_count',
];
export const serviceSettingDefaults: RecordValue = {
  deploymentConfiguration: {},
  networkConfiguration: {},
  capacityProviderStrategy: [],
  launchType: null,
  platformVersion: null,
  schedulingStrategy: 'REPLICA',
};
/** Require an object without accepting serialized or model-inferred values. */
export function deploymentRecord(value: unknown): RecordValue {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('배포 계약 객체가 필요합니다.');
  return value as RecordValue;
}
/** Compare JSON values independently of object key order, preserving array order. */
export function sameDeploymentValue(a: unknown, b: unknown): boolean {
  const sorted = (v: unknown): unknown =>
    Array.isArray(v)
      ? v.map(sorted)
      : v && typeof v === 'object'
        ? Object.fromEntries(
            Object.entries(v)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([k, x]) => [k, sorted(x)]),
          )
        : v;
  return JSON.stringify(sorted(a)) === JSON.stringify(sorted(b));
}
function requireContract(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Error(message);
}
function exact(
  value: unknown,
  keys: string[],
  optional: string[] = [],
): RecordValue {
  const record = deploymentRecord(value);
  requireContract(
    keys.every((k) => Object.hasOwn(record, k)) &&
      Object.keys(record).every((k) => [...keys, ...optional].includes(k)),
    '배포 계약 필드가 누락되었거나 지원하지 않는 필드가 있습니다.',
  );
  return record;
}
function fixed(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    !!value.trim() &&
    !/\$(?!\.[A-Za-z_])|`|<[^>]+>|\{\{|\b(?:TODO|TBD|PLACEHOLDER)\b/i.test(
      value,
    )
  );
}
function scope(value: RecordValue): void {
  requireContract(
    deploymentScopeFields
      .filter((k) => k !== 'desired_count')
      .every((k) => fixed(value[k])),
    '배포 대상이 확정되지 않았습니다.',
  );
  requireContract(
    /^\d{12}$/.test(String(value.account_id)) &&
      /^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(String(value.region)),
    '배포 계정 또는 리전이 올바르지 않습니다.',
  );
  const prefix = `arn:aws:ecs:${value.region}:${value.account_id}:`;
  requireContract(
    String(value.cluster).startsWith(prefix + 'cluster/') &&
      String(value.service).startsWith(
        prefix + 'service/' + String(value.cluster).split('cluster/')[1] + '/',
      ),
    '클러스터와 서비스의 계정·리전·소속이 다릅니다.',
  );
  requireContract(
    Number.isInteger(value.desired_count) && Number(value.desired_count) > 0,
    '태스크 수는 양의 정수여야 합니다.',
  );
}
function definition(value: RecordValue, key: string, digest: string): void {
  const prefix = `arn:aws:ecs:${value.region}:${value.account_id}:task-definition/`;
  requireContract(
    typeof value[key] === 'string' &&
      value[key].startsWith(prefix) &&
      /^[A-Za-z0-9_-]+:[1-9][0-9]*$/.test(value[key].slice(prefix.length)) &&
      /^sha256:[0-9a-f]{64}$/.test(String(value[digest])),
    '같은 계정·리전의 고정 태스크 정의 리비전과 이미지 지문이 필요합니다.',
  );
}
/** Validate the guarded command's literal argv; ordinary four options only. */
export function validateDeploymentGuard(
  step: RecordValue,
  argv: string[] | null,
): RecordValue {
  const guard = exact(step.ecs_service_precondition, [
    ...deploymentScopeFields,
    'expected_task_definition',
    'expected_image_digest',
    'expected_deployment_id',
    'service_settings',
  ]);
  scope(guard);
  definition(guard, 'expected_task_definition', 'expected_image_digest');
  requireContract(
    fixed(guard.expected_deployment_id),
    '현재 배포 ID가 필요합니다.',
  );
  const settings = exact(
    guard.service_settings,
    Object.keys(serviceSettingDefaults),
  );
  deploymentRecord(settings.deploymentConfiguration);
  deploymentRecord(settings.networkConfiguration);
  requireContract(
    Array.isArray(settings.capacityProviderStrategy) &&
      settings.schedulingStrategy === 'REPLICA' &&
      ['launchType', 'platformVersion'].every(
        (k) => settings[k] === null || fixed(settings[k]),
      ),
    '지원하지 않는 서비스 설정입니다.',
  );
  requireContract(
    Array.isArray(step.commands) &&
      step.commands.length === 1 &&
      argv?.length === 11 &&
      argv.slice(0, 3).join(' ') === 'aws ecs update-service',
    '롤백은 단일 UpdateService 명령이어야 합니다.',
  );
  const options = Object.fromEntries(
    [3, 5, 7, 9].map((i) => [argv[i], argv[i + 1]]),
  );
  exact(options, ['--cluster', '--service', '--task-definition', '--region']);
  requireContract(
    ['cluster', 'service', 'region'].every(
      (k) => options['--' + k] === guard[k],
    ) && options['--task-definition'] !== guard.expected_task_definition,
    '롤백 명령과 현재 배포 전제가 다릅니다.',
  );
  return guard;
}
/** Validate convergence against its earlier guarded rollback and reader-owned context. */
export function validateDeploymentPair(
  book: RecordValue,
  action: RecordValue,
  value: unknown,
  argv: string[] | null,
): RecordValue {
  const wait = exact(value, [
    ...deploymentScopeFields,
    'action_step_id',
    'task_definition',
    'image_digest',
    'max_wait_seconds',
  ]);
  scope(wait);
  definition(wait, 'task_definition', 'image_digest');
  requireContract(
    fixed(wait.action_step_id) &&
      Number.isInteger(wait.max_wait_seconds) &&
      Number(wait.max_wait_seconds) >= 1 &&
      Number(wait.max_wait_seconds) <= 900,
    '수렴 기준 단계와 1~900초(최대 15분) 대기 한도가 필요합니다.',
  );
  const guard = validateDeploymentGuard(action, argv);
  const targetIndex = argv!.indexOf('--task-definition');
  requireContract(
    deploymentScopeFields.every((k) =>
      sameDeploymentValue(guard[k], wait[k]),
    ) &&
      argv![targetIndex + 1] === wait.task_definition &&
      guard.expected_image_digest !== wait.image_digest,
    '수렴 대상이 롤백 명령·전제와 다릅니다.',
  );
  const context = exact(
    book.rollback_context,
    ['baseline_ref', 'scope', 'normal', 'current', 'service_settings'],
    ['write_accounting'],
  );
  const ref = exact(context.baseline_ref, ['bucket', 'key', 'sha256']);
  requireContract(
    Object.values(ref).every((v) => typeof v === 'string' && v.trim()) &&
      /^(?:sha256:)?[a-f0-9]{64}$/.test(String(ref.sha256)),
    '정상 기준의 저장 위치와 지문이 필요합니다.',
  );
  const storedScope = exact(context.scope, [
    'account_id',
    'region',
    'cluster_arn',
    'service_arn',
    'service_name',
    'container_name',
    'desired_count',
    'log_group',
  ]);
  const mapped = {
    ...storedScope,
    cluster: storedScope.cluster_arn,
    service: storedScope.service_arn,
  };
  requireContract(
    deploymentScopeFields.every((k) =>
      sameDeploymentValue(mapped[k as keyof typeof mapped], guard[k]),
    ) &&
      storedScope.service_name === String(guard.service).split('/').at(-1) &&
      typeof storedScope.log_group === 'string' &&
      storedScope.log_group.trim(),
    'reader가 확인한 서비스 범위와 다릅니다.',
  );
  const normal = exact(context.normal, ['task_definition_arn', 'image_digest']);
  const current = exact(context.current, [
    'task_definition_arn',
    'image_digest',
    'deployment_id',
  ]);
  requireContract(
    normal.task_definition_arn === wait.task_definition &&
      normal.image_digest === wait.image_digest &&
      current.task_definition_arn === guard.expected_task_definition &&
      current.image_digest === guard.expected_image_digest &&
      current.deployment_id === guard.expected_deployment_id &&
      sameDeploymentValue(context.service_settings, guard.service_settings),
    '모델의 배포 전제 또는 정상 대상이 reader 확인 결과와 다릅니다.',
  );
  if ('write_accounting' in context) validateWriteAccounting(context);
  return wait;
}
/** Keep metric ServiceName independent of the physical ECS service name. */
export function validateWriteAccounting(context: RecordValue): void {
  const keys = [
    'namespace',
    'dimensions',
    'attempts_metric',
    'failures_metric',
    'operation_kind',
    'accounting',
    'source_ref',
    'task_definition_arn',
    'image_digest',
  ];
  const descriptor = exact(context.write_accounting, keys);
  const dimensions = exact(descriptor.dimensions, ['ServiceName']);
  const normal = deploymentRecord(context.normal),
    storedScope = deploymentRecord(context.scope);
  requireContract(
    keys
      .filter((k) => k !== 'dimensions')
      .every(
        (k) => typeof descriptor[k] === 'string' && descriptor[k].trim(),
      ) &&
      typeof dimensions.ServiceName === 'string' &&
      dimensions.ServiceName.trim() &&
      descriptor.operation_kind === 'write' &&
      descriptor.accounting === 'completed' &&
      ['task_definition_arn', 'image_digest'].every(
        (k) => descriptor[k] === normal[k],
      ) &&
      /^sha256:[a-f0-9]{64}$/.test(String(descriptor.image_digest)),
    '정상 쓰기 집계 근거가 올바르지 않습니다.',
  );
  const prefix = `cloudwatch-logs://${storedScope.log_group}/`,
    source = String(descriptor.source_ref);
  const [stream, ...event] = source.slice(prefix.length).split('#');
  const parts = stream!.split('/');
  requireContract(
    source.startsWith(prefix) &&
      event.join('#') &&
      parts.length >= 3 &&
      parts.at(-2) === storedScope.container_name &&
      parts.at(-1),
    '정상 쓰기 집계의 로그 출처가 다릅니다.',
  );
}
