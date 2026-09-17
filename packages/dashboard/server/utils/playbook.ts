import {
  validateDeploymentGuard,
  validateDeploymentPair,
  sameDeploymentValue,
} from './deploymentContract.ts';
import commandModels from './runbook-command-models.json' with { type: 'json' };
import earlyReadOperations from './early-recovery-read-operations.json' with { type: 'json' };

type DataRecord = Record<string, unknown>;
const MODEL_REQUIREMENTS: Record<string, readonly string[]> =
  commandModels.required_options;

export interface PlaybookExecutionStep {
  step_id: string;
  intent?: string;
  action: string;
  success_criteria: string;
  commands?: string[];
  deployment_wait?: Record<string, unknown> | null;
  ecs_service_precondition?: Record<string, unknown> | null;
  metric_wait?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface ResolvedPlaybook {
  playbook: DataRecord;
  source: 'revision' | 'session' | 'span' | 'legacy-span';
  sourceItem: DataRecord;
}

export interface PlaybookValidation {
  valid: boolean;
  steps: PlaybookExecutionStep[];
  reason: string;
}

function asObject(value: unknown): DataRecord | null {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    return value as DataRecord;
  }
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value);
    return parsed !== null &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed)
      ? (parsed as DataRecord)
      : null;
  } catch {
    return null;
  }
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function itemEngine(item: DataRecord): string {
  const explicit = text(item.engine);
  if (explicit) return explicit;
  const sortKey = text(item.SK);
  if (sortKey.startsWith('SPAN#') || sortKey === 'SESSION') return 'strands';
  return sortKey.split('#')[0] ?? '';
}

function belongsToPlaybook(
  playbook: DataRecord,
  expectedPlaybookId: string,
): boolean {
  if (!expectedPlaybookId) return true;
  return text(playbook.playbook_id) === expectedPlaybookId;
}

function spanPlaybook(item: DataRecord): DataRecord | null {
  if (item.span_type !== 'PLAYBOOK') return null;
  return asObject(item.metadata);
}

/**
 * Resolve the one playbook this completed session currently owns.
 *
 * A revision is current only when it names the session's current playbook id.
 * CC stores its exact completed-session playbook as JSON. Strands records an
 * exact span pointer; old sessions without that pointer are accepted only when
 * one candidate remains after engine and playbook-id filtering.
 */
export function resolveCurrentPlaybook(
  items: DataRecord[],
  session: DataRecord,
  engine: string,
): ResolvedPlaybook | null {
  if (text(session.state) !== 'COMPLETED') return null;
  const playbookId = text(session.playbook_id);

  if (playbookId) {
    const revision = items.find(
      (item) =>
        text(item.SK) === `${engine}#PLAYBOOK_REVISION` &&
        text(item.playbook_id) === playbookId,
    );
    const revised = asObject(revision?.playbook);
    if (revision) {
      // A recorded current revision owns the decision even when unreadable.
      // Falling back here would approve commands superseded by that revision.
      const published =
        !('publication_status' in revision) ||
        revision.publication_status === 'PUBLISHED';
      const retained =
        !('ttl' in revision) ||
        Number(revision.ttl) > Math.floor(Date.now() / 1000);
      return published &&
        retained &&
        revised &&
        belongsToPlaybook(revised, playbookId)
        ? { playbook: revised, source: 'revision', sourceItem: revision }
        : null;
    }
  }

  if (
    engine === 'headless-codex' ||
    engine === 'codex-headless' ||
    engine === 'cc-headless'
  ) {
    const persisted = asObject(session.playbook);
    if (persisted && belongsToPlaybook(persisted, playbookId)) {
      return { playbook: persisted, source: 'session', sourceItem: session };
    }
    return null;
  }

  const spanId = text(session.playbook_span_id);
  if (spanId) {
    const span = items.find((item) => {
      const sortKey = text(item.SK);
      return (
        (sortKey === `${engine}#SPAN#${spanId}` ||
          sortKey === `SPAN#${spanId}`) &&
        itemEngine(item) === engine
      );
    });
    const playbook = spanPlaybook(span ?? {});
    if (span && playbook && belongsToPlaybook(playbook, playbookId)) {
      return { playbook, source: 'span', sourceItem: span };
    }
    return null;
  }

  const candidates = items.flatMap((item) => {
    if (itemEngine(item) !== engine) return [];
    const playbook = spanPlaybook(item);
    if (!playbook || !belongsToPlaybook(playbook, playbookId)) return [];
    return [{ item, playbook }];
  });
  if (candidates.length !== 1) return null;

  return {
    playbook: candidates[0]!.playbook,
    source: 'legacy-span',
    sourceItem: candidates[0]!.item,
  };
}

// Validation never rewrites approved values: hashes and the worker compare the
// original command bytes, not a reconstructed shell command.
const REGION = /^[a-z]{2}(?:-[a-z]+)+-\d+$/;
// A quoted CloudWatch filter may contain the literal JSON root "$.field".
// Environment/positional variables and substitutions are still unresolved input.
const UNFIXED =
  /\$(?!\.[A-Za-z_])|`|<[^>]+>|\{\{|\b(?:TODO|TBD|PLACEHOLDER)\b/i;

function object(value: unknown): DataRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as DataRecord)
    : null;
}

function fixedText(value: unknown, coordinate = false): value is string {
  return (
    typeof value === 'string' &&
    Boolean(value.trim()) &&
    !UNFIXED.test(value) &&
    (!coordinate || (value.length <= 1024 && !/[\r\n\0]/.test(value)))
  );
}

/** Literal POSIX argv subset. No interpolation, operators, comments or file inputs.
 * Quotes and escapes match shlex.split; malformed quotes fail closed.
 */
function commandArgv(command: string): string[] | null {
  if (!fixedText(command) || /[\x00-\x08\x0a-\x1f\x7f]/.test(command))
    return null;
  const argv: string[] = [];
  let token = '';
  let quote = '';
  let started = false;
  for (let i = 0; i < command.length; i++) {
    const char = command[i]!;
    if (quote === "'") {
      if (char === "'") quote = '';
      else token += char;
    } else if (char === '\\') {
      const next = command[++i];
      if (next === undefined) return null;
      if (quote === '"' && next !== '"' && next !== '\\') token += '\\';
      token += next;
      started = true;
    } else if (quote === '"') {
      if (char === '"') quote = '';
      else token += char;
    } else if (char === '"' || char === "'") {
      quote = char;
      started = true;
    } else if (char === ' ' || char === '\t') {
      if (started) argv.push(token);
      token = '';
      started = false;
    } else {
      if (/[;&|<>#]/.test(char)) return null;
      token += char;
      started = true;
    }
  }
  if (quote) return null;
  if (started) argv.push(token);
  return argv;
}

// These selectors are part of the shared ECS execution contract, not a service
// allowlist. Other AWS services/operations retain the worker's existing scope.
function requiredTargetOptions(service: string, operation: string): string[] {
  if (service !== 'ecs') return [];
  if (operation === 'stop-task') return ['--cluster', '--task'];
  if (operation === 'describe-tasks') return ['--cluster', '--tasks'];
  if (operation === 'update-service') return ['--cluster', '--service'];
  if (operation === 'describe-services') return ['--cluster', '--services'];
  return [];
}
const FORBIDDEN_OPTIONS = new Set([
  '--profile',
  '--endpoint-url',
  '--cli-input-json',
  '--cli-input-yaml',
  '--generate-cli-skeleton',
  '--debug',
  '--no-sign-request',
  '--no-verify-ssl',
]);

interface CommandScope {
  operation: string;
  region: string;
  options: Map<string, string[]>;
}

function inspectCommand(command: unknown): CommandScope | null {
  if (typeof command !== 'string') return null;
  const argv = commandArgv(command);
  if (!argv || argv.length < 4 || argv[0] !== 'aws') return null;
  if (
    !/^[a-z0-9][a-z0-9-]*$/.test(argv[1] ?? '') ||
    !/^[a-z0-9][a-z0-9-]*$/.test(argv[2] ?? '')
  )
    return null;
  const operation = `${argv[1]} ${argv[2]}`;
  // Full locked SDK catalogue; no hand-picked operation permission boundary.
  if (!Object.hasOwn(MODEL_REQUIREMENTS, operation)) return null;
  const required = [
    ...MODEL_REQUIREMENTS[operation]!,
    ...requiredTargetOptions(argv[1]!, argv[2]!),
  ];
  const options = new Map<string, string[]>();
  let active = '';
  for (const token of argv.slice(3)) {
    if (token.startsWith('--')) {
      const equal = token.indexOf('=');
      const name = equal < 0 ? token : token.slice(0, equal);
      const canonical = name.startsWith('--no-') ? `--${name.slice(5)}` : name;
      if (
        !/^--[a-z][a-z0-9-]*$/.test(name) ||
        FORBIDDEN_OPTIONS.has(name) ||
        options.has(name) ||
        options.has(canonical) ||
        options.has(`--no-${name.slice(2)}`)
      )
        return null;
      options.set(name, equal < 0 ? [] : [token.slice(equal + 1)]);
      active = name;
    } else {
      if (
        !active ||
        !token ||
        token.startsWith('-') ||
        /^(?:file|fileb):\/\//.test(token)
      )
        return null;
      options.get(active)!.push(token);
    }
  }
  for (const values of options.values()) {
    // The AWS CLI/model owns option arity: a zero-value flag is not evidence
    // of an incomplete command, and no hand-maintained flag allowlist is used.
    if (
      values.some(
        (value) => !fixedText(value) || /^(?:file|fileb):\/\//.test(value),
      )
    )
      return null;
  }
  const region = options.get('--region');
  if (!region || region.length !== 1 || !REGION.test(region[0]!)) return null;
  if (required.some((name) => !options.get(name)?.length)) return null;
  for (const name of [
    '--task',
    '--cluster',
    '--service',
    '--function-name',
    '--db-instance-identifier',
    '--db-cluster-identifier',
  ]) {
    if (options.has(name) && options.get(name)!.length !== 1) return null;
  }
  return { operation, region: region[0]!, options };
}

export function validateExecutablePlaybook(
  playbook: DataRecord | null,
): PlaybookValidation {
  const reject = (reason: string): PlaybookValidation => ({
    valid: false,
    steps: [],
    reason,
  });
  const rawSteps = playbook?.execution_steps;
  if (!Array.isArray(rawSteps) || !rawSteps.length)
    return reject('플레이북에 실행 절차가 없습니다.');
  const steps: PlaybookExecutionStep[] = [];
  const ids = new Set<string>();
  const priorActions = new Map<string, CommandScope[]>();
  const priorSteps = new Map<string, DataRecord>();
  const deployments = new Map<string, DataRecord>();
  const fields = new Set([
    'step_id',
    'intent',
    'action',
    'success_criteria',
    'commands',
    'metric_wait',
    'deployment_wait',
    'ecs_service_precondition',
  ]);
  for (const entry of rawSteps) {
    const step = object(entry);
    if (!step || Object.keys(step).some((key) => !fields.has(key)))
      return reject('실행 절차의 형식 또는 필드가 올바르지 않습니다.');
    const stepId = step.step_id;
    if (
      !fixedText(stepId, true) ||
      stepId !== stepId.trim() ||
      ids.has(stepId) ||
      !fixedText(step.action) ||
      !fixedText(step.success_criteria)
    ) {
      return reject(
        '각 단계에는 중복되지 않는 식별자와 작업, 성공 판정 기준이 필요합니다.',
      );
    }
    if ('commands' in step && !Array.isArray(step.commands))
      return reject('명령 목록의 형식이 올바르지 않습니다.');
    const commands = Array.isArray(step.commands) ? step.commands : [];
    const hasWait = step.metric_wait !== undefined && step.metric_wait !== null;
    const hasDeployment =
      step.deployment_wait !== undefined && step.deployment_wait !== null;
    if (
      [Boolean(commands.length), hasWait, hasDeployment].filter(Boolean)
        .length !== 1
    )
      return reject(
        '각 단계에는 확정된 명령 목록 또는 사후 관측 설정이 필요합니다. 명령 없는 과거 계획은 새 분석이 필요합니다.',
      );
    try {
      if (step.ecs_service_precondition != null)
        validateDeploymentGuard(step, commandArgv(String(commands[0] ?? '')));
      if (hasDeployment) {
        const wait = object(step.deployment_wait);
        const action = priorSteps.get(String(wait?.action_step_id));
        if (
          !action ||
          [...deployments.values()].some(
            (d) => d.action_step_id === wait?.action_step_id,
          )
        )
          throw new Error(
            '배포 대기는 중복되지 않는 선행 롤백을 참조해야 합니다.',
          );
        deployments.set(
          stepId,
          validateDeploymentPair(
            playbook!,
            action,
            wait,
            commandArgv(String((action.commands as string[])[0])),
          ),
        );
      }
    } catch (error) {
      return reject(
        error instanceof Error
          ? error.message
          : '배포 계약이 올바르지 않습니다.',
      );
    }
    if (commands.length) {
      const inspected = commands.map(inspectCommand);
      if (inspected.some((value) => !value))
        return reject(
          '명령의 리전·대상·형식이 올바르지 않습니다. 중복 옵션, 인자 덮어쓰기와 미확정 값은 승인할 수 없습니다.',
        );
      if (
        playbook?.rollback_context != null &&
        inspected.some((c) => c?.operation === 'ecs update-service') &&
        !step.ecs_service_precondition
      )
        return reject(
          'reader 문맥이 있는 서비스 갱신에는 완전한 배포 전제가 필요합니다.',
        );
      priorActions.set(stepId, inspected as CommandScope[]);
    } else if (hasWait) {
      const wait = object(step.metric_wait);
      if (
        !wait ||
        !validMetricWait(wait, priorActions, step.success_criteria, deployments)
      )
        return reject(
          '사후 관측의 선행 조치, 메트릭 좌표, 알람, 성공 기준 또는 대기 한도가 올바르지 않습니다.',
        );
      if (
        object(wait.completed_work_evidence)?.record_index ===
          'approved_context' &&
        !validApprovedAccounting(playbook!, wait, deployments)
      )
        return reject(
          '승인 문맥의 쓰기 집계 근거가 없거나 관측 지표·복원 배포와 일치하지 않습니다.',
        );
    }
    priorSteps.set(stepId, step);
    ids.add(stepId);
    steps.push(entry as PlaybookExecutionStep);
  }
  for (const step of priorSteps.values()) {
    if (
      step.ecs_service_precondition &&
      [...deployments.values()].filter((d) => d.action_step_id === step.step_id)
        .length !== 1
    )
      return reject('롤백에는 정확히 하나의 후속 배포 수렴 단계가 필요합니다.');
  }
  for (const id of deployments.keys()) {
    if (!steps.some((s) => s.metric_wait?.deployment_step_id === id))
      return reject(
        '배포 수렴 뒤 같은 배포를 기준으로 하는 필수 사후 지표 관측이 필요합니다.',
      );
  }
  return { valid: true, steps, reason: '' };
}

/** Reject an unresolvable reader-owned accounting reference before approval writes.
 * Earlier deployment-pair validation has already checked its guard and normal
 * context. Match the binder's metric/image relationship without rewriting input;
 * unreferenced descriptors and future numeric journal references keep old rules.
 */
function validApprovedAccounting(
  playbook: DataRecord,
  wait: DataRecord,
  deployments: Map<string, DataRecord>,
): boolean {
  const descriptor = object(
    object(playbook.rollback_context)?.write_accounting,
  );
  const deployment = deployments.get(String(wait.deployment_step_id));
  const metrics = object(wait.metrics);
  const attempts = object(metrics?.attempts);
  const failures = object(metrics?.failures);
  return Boolean(
    descriptor &&
    deployment &&
    attempts &&
    failures &&
    descriptor.task_definition_arn === deployment.task_definition &&
    descriptor.image_digest === deployment.image_digest &&
    deployment.region === wait.region &&
    descriptor.namespace === attempts.namespace &&
    sameDeploymentValue(descriptor.dimensions, attempts.dimensions) &&
    descriptor.attempts_metric === attempts.metric_name &&
    descriptor.failures_metric === failures.metric_name &&
    descriptor.operation_kind === 'write' &&
    descriptor.accounting === 'completed',
  );
}

function validMetricWait(
  wait: DataRecord,
  priorActions: Map<string, CommandScope[]>,
  criterion: string,
  deployments: Map<string, DataRecord>,
): boolean {
  const allowed = new Set([
    'action_step_id',
    'deployment_step_id',
    'metrics',
    'failure_alarm_name',
    'latency_alarm_name',
    'region',
    'max_wait_seconds',
    'completed_work_evidence',
  ]);
  if (Object.keys(wait).some((key) => !allowed.has(key))) return false;
  if (
    'action_step_id' in wait === 'deployment_step_id' in wait ||
    !fixedText(wait.action_step_id ?? wait.deployment_step_id, true) ||
    !fixedText(wait.failure_alarm_name, true) ||
    !fixedText(wait.region, true) ||
    !REGION.test(wait.region)
  )
    return false;
  // This tool anchors its fixed bins to an actual StopTask, not another wait/read.
  if (
    'deployment_step_id' in wait
      ? deployments.get(String(wait.deployment_step_id))?.region !== wait.region
      : !priorActions.get(String(wait.action_step_id))?.some((command) => {
          if (
            command.operation !== 'ecs stop-task' ||
            command.region !== wait.region ||
            !intactObservation(command)
          )
            return false;
          const task = command.options.get('--task')?.[0] ?? '';
          const cluster = command.options.get('--cluster')?.[0] ?? '';
          const match = /^arn:aws:ecs:([^:]+):(\d{12}):task\/(.+)$/.exec(task);
          if (!match || match[1] !== wait.region) return false;
          return (
            !cluster.startsWith('arn:') ||
            cluster.startsWith(`arn:aws:ecs:${match[1]}:${match[2]}:cluster/`)
          );
        })
  )
    return false;
  // Runtime requires intact current-execution discovery before the fixed wait.
  const observations = [...priorActions.values()]
    .flat()
    .filter(
      (command) => command.region === wait.region && intactObservation(command),
    );
  for (const operation of [
    'cloudwatch list-metrics',
    'cloudwatch describe-alarms',
  ]) {
    if (!observations.some((command) => command.operation === operation))
      return false;
  }
  const seconds =
    wait.max_wait_seconds === undefined ? 900 : wait.max_wait_seconds;
  if (
    typeof seconds !== 'number' ||
    !Number.isInteger(seconds) ||
    seconds < 1 ||
    seconds > 900
  )
    return false;
  const metrics = object(wait.metrics);
  if (
    !metrics ||
    !('attempts' in metrics) ||
    !('failures' in metrics) ||
    Object.keys(metrics).some(
      (key) => !['attempts', 'failures', 'latency'].includes(key),
    )
  )
    return false;
  if ('latency' in metrics !== Boolean(wait.latency_alarm_name)) return false;
  if (
    'latency_alarm_name' in wait &&
    typeof wait.latency_alarm_name !== 'string'
  )
    return false;
  if (wait.latency_alarm_name && !fixedText(wait.latency_alarm_name, true))
    return false;
  const names = new Set<string>();
  let identity = '';
  for (const value of Object.values(metrics)) {
    const metric = object(value);
    if (
      !metric ||
      Object.keys(metric).sort().join(',') !==
        'dimensions,metric_name,namespace' ||
      !fixedText(metric.namespace, true) ||
      !fixedText(metric.metric_name, true)
    )
      return false;
    const dimensions = object(metric.dimensions);
    if (
      !dimensions ||
      Object.keys(dimensions).length < 1 ||
      Object.keys(dimensions).length > 30 ||
      Object.entries(dimensions).some(
        ([key, value]) => !fixedText(key, true) || !fixedText(value, true),
      )
    )
      return false;
    if (names.has(metric.metric_name)) return false;
    names.add(metric.metric_name);
    const current = JSON.stringify([
      metric.namespace,
      Object.entries(dimensions).sort(),
    ]);
    if (identity && identity !== current) return false;
    identity = current;
  }
  if (
    !criterion.includes(String(object(metrics.failures)?.metric_name)) ||
    !criterion.includes(wait.failure_alarm_name)
  )
    return false;
  if (
    metrics.latency &&
    (!criterion.includes(String(object(metrics.latency)?.metric_name)) ||
      !criterion.includes(String(wait.latency_alarm_name)))
  )
    return false;
  if (
    wait.completed_work_evidence !== undefined &&
    wait.completed_work_evidence !== null
  ) {
    const ref = object(wait.completed_work_evidence);
    if (
      !ref ||
      Object.keys(ref).sort().join(',') !== 'json_pointer,record_index' ||
      typeof ref.json_pointer !== 'string' ||
      !ref.json_pointer.startsWith('/')
    )
      return false;
    // Runtime _completed_accounting resolves this single reader-owned context path.
    // Numeric journal references retain their own observed-document JSON pointers.
    if (
      ref.record_index === 'approved_context' &&
      ref.json_pointer !== '/playbook/rollback_context/write_accounting'
    )
      return false;
    if (
      ref.record_index !== 'approved_context' &&
      (typeof ref.record_index !== 'number' ||
        !Number.isInteger(ref.record_index) ||
        ref.record_index < 0)
    )
      return false;
  }
  return true;
}

function intactObservation(command: CommandScope): boolean {
  return (
    !command.options.has('--query') &&
    (!command.options.has('--output') ||
      command.options.get('--output')?.join('') === 'json')
  );
}

/** Preserve recorded prose for inspection even when it cannot authorize execution. */
export function readableExecutionSteps(
  playbook: DataRecord,
): PlaybookExecutionStep[] {
  if (!Array.isArray(playbook.execution_steps)) return [];
  return playbook.execution_steps.flatMap((entry) => {
    const step = asObject(entry);
    if (!step) return [];
    return [
      {
        ...step,
        raw_operation: { ...step },
        step_id: text(step.step_id),
        intent: text(step.intent),
        action: text(step.action),
        success_criteria: text(step.success_criteria),
        commands: Array.isArray(step.commands)
          ? step.commands.filter(
              (value): value is string => typeof value === 'string',
            )
          : [],
        metric_wait: asObject(step.metric_wait),
        deployment_wait: asObject(step.deployment_wait),
        ecs_service_precondition: asObject(step.ecs_service_precondition),
      },
    ];
  });
}

export function findSessionForEngine(
  items: DataRecord[],
  engine: string,
): DataRecord | null {
  return (
    items.find((item) => {
      const sortKey = text(item.SK);
      const isSession = sortKey === 'SESSION' || sortKey.endsWith('#SESSION');
      return isSession && itemEngine(item) === engine;
    }) ?? null
  );
}

export function countExecutionSteps(
  items: DataRecord[],
  engine: string,
): number {
  const session = findSessionForEngine(items, engine);
  if (!session) return 0;
  if (session.confirmed !== true) return 0;
  const resolved = resolveCurrentPlaybook(items, session, engine);
  return validateExecutablePlaybook(resolved?.playbook ?? null).steps.length;
}

/** Early unconfirmed recovery permits exactly one pinned rollback and an exhaustive set of read operations.
 * The completed/confirmed legacy contract intentionally continues to use validateExecutablePlaybook.
 */
export function validateEarlyRecoveryPlaybook(
  playbook: DataRecord | null,
): PlaybookValidation {
  const validation = validateExecutablePlaybook(playbook);
  if (!validation.valid) return validation;
  const reject = (reason: string): PlaybookValidation => ({
    valid: false,
    steps: [],
    reason,
  });
  if (!playbook?.rollback_context)
    return reject('조기 정상화에는 서버가 고정한 롤백 문맥이 필요합니다.');
  const readFlags: Record<string, readonly string[]> = earlyReadOperations;
  const reads = new Set(Object.keys(readFlags));
  const context = playbook.rollback_context as Record<string, any>;
  const scope = context.scope;
  const matches = (values: string[] | undefined, ...choices: unknown[]) =>
    values?.length === 1 && choices.includes(values[0]);
  const rollbacks: string[] = [];
  for (const step of validation.steps) {
    for (const command of step.commands ?? []) {
      const parsed = inspectCommand(command);
      if (!parsed)
        return reject('조기 정상화 명령을 정확히 해석할 수 없습니다.');
      if (parsed.operation === 'ecs update-service') {
        if (!step.ecs_service_precondition)
          return reject('조기 롤백에는 고정 배포 전제가 필요합니다.');
        rollbacks.push(step.step_id);
      } else {
        if (!reads.has(parsed.operation) || parsed.region !== scope.region)
          return reject(
            `조기 정상화에서 허용되지 않은 작업 또는 리전입니다: ${parsed.operation}`,
          );
        const allowed = new Set([
          ...readFlags[parsed.operation]!,
          '--region',
          '--query',
          '--output',
        ]);
        for (const [flag, values] of parsed.options) {
          if (!allowed.has(flag) || !values.length)
            return reject('조기 읽기 작업의 인자가 허용되지 않습니다.');
        }
        if (
          parsed.options.has('--output') &&
          !matches(parsed.options.get('--output'), 'json')
        )
          return reject('조기 읽기 결과는 JSON이어야 합니다.');
        const [service, operation] = parsed.operation.split(' ');
        if (
          service === 'ecs' &&
          operation !== 'describe-task-definition' &&
          !matches(
            parsed.options.get('--cluster'),
            scope.cluster_arn,
            String(scope.cluster_arn).split('/').at(-1),
          )
        )
          return reject('조기 읽기 클러스터가 고정 대상과 다릅니다.');
        if (
          parsed.operation === 'ecs describe-services' &&
          !matches(
            parsed.options.get('--services'),
            scope.service_arn,
            scope.service_name,
          )
        )
          return reject('조기 읽기 서비스가 고정 대상과 다릅니다.');
        if (
          parsed.operation === 'ecs list-tasks' &&
          !matches(
            parsed.options.get('--service-name'),
            scope.service_arn,
            scope.service_name,
          )
        )
          return reject('조기 태스크 목록은 고정 서비스를 지정해야 합니다.');
        if (
          parsed.operation === 'ecs describe-task-definition' &&
          !matches(
            parsed.options.get('--task-definition'),
            context.normal.task_definition_arn,
            context.current.task_definition_arn,
          )
        )
          return reject('조기 태스크 정의 조회가 고정 대상과 다릅니다.');
        if (
          service === 'logs' &&
          !matches(parsed.options.get('--log-group-name'), scope.log_group)
        )
          return reject('조기 로그 조회가 고정 로그 그룹과 다릅니다.');
        if (
          (service === 'logs' ||
            ['get-metric-data', 'get-metric-statistics'].includes(
              operation!,
            )) &&
          (!parsed.options.get('--start-time')?.length ||
            !parsed.options.get('--end-time')?.length)
        )
          return reject(
            '조기 관측에는 고정된 조회 시작·종료 시각이 필요합니다.',
          );
      }
    }
  }
  const waits = validation.steps.filter((step) => step.deployment_wait);
  if (
    rollbacks.length !== 1 ||
    waits.length !== 1 ||
    waits[0]!.deployment_wait?.action_step_id !== rollbacks[0]
  )
    return reject(
      '조기 정상화는 고정 UpdateService 1개와 그 배포 수렴 대기만 허용합니다.',
    );
  return validation;
}
