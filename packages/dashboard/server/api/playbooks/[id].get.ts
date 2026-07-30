import { QueryCommand } from '@aws-sdk/lib-dynamodb';

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id');
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing RCA id' });
  }

  const query = getQuery(event);
  const engineFilter = (query.engine as string) || '';

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk',
      ExpressionAttributeValues: { ':pk': `RCA#${id}` },
    }),
  );

  const items = result.Items ?? [];

  const span = items.find((i) => {
    const sk = (i.SK as string) || '';
    const isSpan = sk.includes('#SPAN#') || sk.startsWith('SPAN#');
    if (!isSpan) return false;
    if (i.span_type !== 'PLAYBOOK') return false;
    if (!engineFilter) return true;
    return parseEngine(sk) === engineFilter;
  });

  if (!span) {
    throw createError({ statusCode: 404, statusMessage: 'Playbook not found' });
  }

  const metadata = (span.metadata as Record<string, unknown>) || {};

  // A retrospective revision, when one exists, is the current procedure and the
  // basis of the next execution — so it wins over what analysis first recorded.
  const revision = items.find((i) => {
    const sk = (i.SK as string) || '';
    if (!sk.endsWith('#PLAYBOOK_REVISION')) return false;
    if (!engineFilter) return true;
    return parseEngine(sk) === engineFilter;
  });
  const revised = safeParse(revision?.playbook);

  function text(field: string): string {
    const fromRevision = revised?.[field];
    if (typeof fromRevision === 'string' && fromRevision) return fromRevision;
    return (metadata[field] as string) || '';
  }

  function list(field: string): string[] {
    const fromRevision = revised?.[field];
    if (Array.isArray(fromRevision)) {
      return fromRevision.filter(
        (entry): entry is string => typeof entry === 'string',
      );
    }
    return (metadata[field] as string[]) || [];
  }

  return {
    rcaId: id,
    spanStatus: (span.span_status as string) || 'UNKNOWN',
    durationMs: (span.duration_ms as number) ?? null,
    error: (span.error as string) || null,
    outputSummary: (span.output_summary as string) || '',
    playbook_id: text('playbook_id'),
    failure_type: text('failure_type'),
    symptom_pattern: text('symptom_pattern'),
    severity_criteria: text('severity_criteria'),
    verification_steps: list('verification_steps'),
    execution_steps: readExecutionSteps(
      revised?.execution_steps ?? metadata.execution_steps,
    ),
    temporary_mitigation: text('temporary_mitigation'),
    permanent_remediation: text('permanent_remediation'),
    escalation_criteria: text('escalation_criteria'),
    prevention_measures: list('prevention_measures'),
    related_metrics: list('related_metrics'),
    tags: list('tags'),
    // DRAFT until an execution and its retrospective have exercised it.
    verification_status: text('verification_status') || 'DRAFT',
    revisedByExecutionId: (revision?.revised_by_execution_id as string) || '',
  };
});

export interface PlaybookExecutionStep {
  step_id: string;
  intent: string;
  action: string;
  success_criteria: string;
}

function readExecutionSteps(raw: unknown): PlaybookExecutionStep[] {
  if (!Array.isArray(raw)) return [];
  const steps: PlaybookExecutionStep[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry))
      continue;
    const step = entry as Record<string, unknown>;
    const stepId = typeof step.step_id === 'string' ? step.step_id.trim() : '';
    // A step with no identifier cannot be pointed at by evidence or corrected by
    // a retrospective, so it is not shown as an approvable step.
    if (!stepId) continue;
    steps.push({
      step_id: stepId,
      intent: typeof step.intent === 'string' ? step.intent : '',
      action: typeof step.action === 'string' ? step.action : '',
      success_criteria:
        typeof step.success_criteria === 'string' ? step.success_criteria : '',
    });
  }
  return steps;
}

function safeParse(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value);
    return parsed !== null &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}
