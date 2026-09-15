import commandModels from './runbook-command-models.json' with { type: 'json' };

type DataRecord = Record<string, unknown>;
const MODEL_REQUIREMENTS: Record<string, readonly string[]> =
  commandModels.required_options;

export interface PlaybookExecutionStep {
  step_id: string;
  intent?: string;
  action: string;
  success_criteria: string;
  commands?: string[];
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
  const fields = new Set([
    'step_id',
    'intent',
    'action',
    'success_criteria',
    'commands',
    'metric_wait',
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
    if (Boolean(commands.length) === hasWait)
      return reject(
        '각 단계에는 확정된 명령 목록 또는 사후 관측 설정이 필요합니다. 명령 없는 과거 계획은 새 분석이 필요합니다.',
      );
    if (commands.length) {
      const inspected = commands.map(inspectCommand);
      if (inspected.some((value) => !value))
        return reject(
          '명령의 리전·대상·형식이 올바르지 않습니다. 중복 옵션, 인자 덮어쓰기와 미확정 값은 승인할 수 없습니다.',
        );
      priorActions.set(stepId, inspected as CommandScope[]);
    } else {
      const wait = object(step.metric_wait);
      if (!wait || !validMetricWait(wait, priorActions, step.success_criteria))
        return reject(
          '사후 관측의 선행 조치, 메트릭 좌표, 알람, 성공 기준 또는 대기 한도가 올바르지 않습니다.',
        );
    }
    ids.add(stepId);
    steps.push(entry as PlaybookExecutionStep);
  }
  return { valid: true, steps, reason: '' };
}

function validMetricWait(
  wait: DataRecord,
  priorActions: Map<string, CommandScope[]>,
  criterion: string,
): boolean {
  const allowed = new Set([
    'action_step_id',
    'metrics',
    'failure_alarm_name',
    'latency_alarm_name',
    'region',
    'max_wait_seconds',
    'completed_work_evidence',
  ]);
  if (Object.keys(wait).some((key) => !allowed.has(key))) return false;
  if (
    !fixedText(wait.action_step_id, true) ||
    !fixedText(wait.failure_alarm_name, true) ||
    !fixedText(wait.region, true) ||
    !REGION.test(wait.region)
  )
    return false;
  // This tool anchors its fixed bins to an actual StopTask, not another wait/read.
  if (
    !priorActions.get(wait.action_step_id)?.some((command) => {
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
    wait.max_wait_seconds === undefined ? 300 : wait.max_wait_seconds;
  if (
    typeof seconds !== 'number' ||
    !Number.isInteger(seconds) ||
    seconds < 1 ||
    seconds > 300
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
