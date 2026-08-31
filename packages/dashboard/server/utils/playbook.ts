type DataRecord = Record<string, unknown>;

export interface PlaybookExecutionStep {
  step_id: string;
  intent?: string;
  action: string;
  success_criteria: string;
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
    if (revision && revised && belongsToPlaybook(revised, playbookId)) {
      return { playbook: revised, source: 'revision', sourceItem: revision };
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

export function validateExecutablePlaybook(
  playbook: DataRecord | null,
): PlaybookValidation {
  const rawSteps = playbook?.execution_steps;
  if (!Array.isArray(rawSteps) || rawSteps.length === 0) {
    return {
      valid: false,
      steps: [],
      reason: '플레이북에 실행 절차가 없습니다.',
    };
  }

  const steps: PlaybookExecutionStep[] = [];
  const stepIds = new Set<string>();
  for (const entry of rawSteps) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
      return {
        valid: false,
        steps: [],
        reason: '실행 절차 항목의 형식이 올바르지 않습니다.',
      };
    }
    const step = entry as DataRecord;
    const stepId = text(step.step_id);
    const action = text(step.action);
    const successCriteria = text(step.success_criteria);
    if (!stepId || !action || !successCriteria) {
      return {
        valid: false,
        steps: [],
        reason:
          '모든 실행 절차에는 step_id, action, success_criteria가 필요합니다.',
      };
    }
    if (stepIds.has(stepId)) {
      return {
        valid: false,
        steps: [],
        reason: `중복된 실행 절차 식별자가 있습니다: ${stepId}`,
      };
    }
    stepIds.add(stepId);
    steps.push(entry as PlaybookExecutionStep);
  }

  return { valid: true, steps, reason: '' };
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
