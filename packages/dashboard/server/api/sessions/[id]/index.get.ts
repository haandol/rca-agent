import { QueryCommand } from '@aws-sdk/lib-dynamodb';

/**
 * One session, with the same fields a list row carries.
 *
 * The report and playbook pages need exactly one session, and they used to fetch
 * the entire list and search it. That worked only while the list was unpaged: a
 * session older than the first page would now be missing, and the page would claim
 * the report did not exist. Reading its own partition is also what these pages
 * were really asking for — the whole archive was never the question.
 *
 * The shape matches a list row on purpose, so both pages read the same field names
 * whichever way they arrived.
 */
export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id');
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing session id' });
  }

  const requestedEngine = getQuery(event).engine;
  const engineFilter =
    typeof requestedEngine === 'string' && isAllowedEngine(requestedEngine)
      ? requestedEngine
      : '';

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk',
      ExpressionAttributeValues: { ':pk': rcaPk(id) },
    }),
  );
  const items = result.Items ?? [];

  const sessionItem = items.find((item) => {
    const sortKey = (item.SK as string) || '';
    if (!isSessionSortKey(sortKey)) return false;
    if (!engineFilter) return true;
    return ((item.engine as string) || parseEngine(sortKey)) === engineFilter;
  });

  if (!sessionItem) {
    throw createError({
      statusCode: 404,
      statusMessage: '세션을 찾을 수 없습니다.',
    });
  }

  const engine =
    (sessionItem.engine as string) || parseEngine(sessionItem.SK as string);

  const executions = items
    .filter((item) => isExecutionItem((item.SK as string) || ''))
    .map(readExecution)
    .filter((execution) => execution.engine === engine);
  const execution = latestExecution(executions);

  const spans = items
    .filter((item) => isSpanSortKey((item.SK as string) || ''))
    .map((item) => ({
      spanType: (item.span_type as string) || '',
      engine: (item.engine as string) || parseEngine((item.SK as string) || ''),
    }))
    .filter((span) => span.engine === engine);

  const state = (sessionItem.state as string) || 'UNKNOWN';
  const stepCount = countExecutionSteps(items, engine);
  const readiness = readinessOf({
    state,
    stepCount,
    hasExecution: executions.length > 0,
  });

  return {
    rcaId: id,
    state,
    readiness,
    readinessLabel: READINESS_LABEL[readiness],
    executionStepCount: stepCount,
    alarmName: (sessionItem.alarm_name as string) || 'N/A',
    alarmArn: (sessionItem.alarm_arn as string) || '',
    rootCause: (sessionItem.root_cause as string) || '',
    confirmed: (sessionItem.confirmed as boolean) ?? false,
    stoppedAt: furthestStage(spans, engine),
    errorReason:
      (sessionItem.error_reason as string) ||
      (sessionItem.outdated_reason as string) ||
      '',
    createdAt: (sessionItem.created_at as string) || '',
    updatedAt: (sessionItem.updated_at as string) || '',
    engine,
    executionState: execution?.state ?? '',
    executionStateLabel: execution?.stateLabel ?? '',
    executionId: execution?.executionId ?? '',
    executionAttempts: executions.length,
    executionBlockedCount: execution?.blockedCount ?? 0,
    retrospectiveStatus: execution?.retrospectiveStatus ?? '',
  };
});
