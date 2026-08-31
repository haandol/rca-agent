import { QueryCommand, type QueryCommandInput } from '@aws-sdk/lib-dynamodb';

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id');
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing RCA id' });
  }

  const requestedEngine = getQuery(event).engine;
  const engine =
    typeof requestedEngine === 'string' ? requestedEngine.trim() : '';
  if (!isAllowedEngine(engine)) {
    throw createError({
      statusCode: 400,
      statusMessage: 'Missing or invalid engine',
    });
  }

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();
  const items: Record<string, unknown>[] = [];
  let startKey: QueryCommandInput['ExclusiveStartKey'];
  do {
    const result = await ddb.send(
      new QueryCommand({
        TableName: config.dynamodbTableName,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': rcaPk(id) },
        ExclusiveStartKey: startKey,
      }),
    );
    items.push(...(result.Items ?? []));
    startKey = result.LastEvaluatedKey;
  } while (startKey);

  const session = findSessionForEngine(items, engine);
  if (!session) {
    throw createError({
      statusCode: 404,
      statusMessage: 'Session not found',
    });
  }
  if (session.state !== 'COMPLETED') {
    throw createError({
      statusCode: 409,
      statusMessage: '분석이 완료되지 않아 플레이북을 조회할 수 없습니다.',
    });
  }

  const resolved = resolveCurrentPlaybook(items, session, engine);
  if (!resolved) {
    throw createError({ statusCode: 404, statusMessage: 'Playbook not found' });
  }

  const playbook = resolved.playbook;
  const validation = validateExecutablePlaybook(playbook);
  const source = resolved.sourceItem;

  return {
    rcaId: id,
    engine,
    spanStatus: (source.span_status as string) || 'UNKNOWN',
    durationMs: (source.duration_ms as number) ?? null,
    error: (source.error as string) || null,
    outputSummary: (source.output_summary as string) || '',
    playbook_id: readText(playbook.playbook_id),
    failure_type: readText(playbook.failure_type),
    symptom_pattern: readText(playbook.symptom_pattern),
    severity_criteria: readText(playbook.severity_criteria),
    verification_steps: readStringList(playbook.verification_steps),
    execution_steps: validation.valid ? validation.steps : [],
    executable: validation.valid,
    validationError: validation.reason,
    temporary_mitigation: readText(playbook.temporary_mitigation),
    permanent_remediation: readText(playbook.permanent_remediation),
    escalation_criteria: readText(playbook.escalation_criteria),
    prevention_measures: readStringList(playbook.prevention_measures),
    related_metrics: readStringList(playbook.related_metrics),
    tags: readStringList(playbook.tags),
    verification_status: readText(playbook.verification_status) || 'DRAFT',
    revisedByExecutionId:
      resolved.source === 'revision'
        ? ((source.revised_by_execution_id as string) ?? '')
        : '',
  };
});

function readText(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function readStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === 'string')
    : [];
}
