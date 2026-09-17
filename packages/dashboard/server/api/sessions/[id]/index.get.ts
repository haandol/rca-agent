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

  const items = await readAnalysisPartition(ddb, config.dynamodbTableName, id);

  // A new-workflow URL may still name the previous owner. Return current parent identity explicitly.
  const currentParent = items.find(
    (item) =>
      item.PK === rcaPk(id) &&
      item.SK === ANALYSIS_SESSION_SK &&
      ['strands', 'headless-codex'].includes(item.engine) &&
      hasAnalysisParts(items, item.engine),
  );
  const sessionItem =
    currentParent ??
    items.find((item) => {
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
    .filter((execution) =>
      hasAnalysisParts(items, engine)
        ? execution.rcaId === id && isAllowedEngine(execution.engine)
        : execution.engine === engine,
    );
  const execution = latestExecution(
    executions,
    hasAnalysisParts(items, engine),
  );

  const spans = items
    .filter((item) => isSpanSortKey((item.SK as string) || ''))
    .map((item) => ({
      spanType: (item.span_type as string) || '',
      engine: (item.engine as string) || parseEngine((item.SK as string) || ''),
    }))
    .filter((span) => span.engine === engine);

  const state = (sessionItem.state as string) || 'UNKNOWN';
  const workflow = hasAnalysisParts(items, engine) ? 'recovery-first-v1' : '';
  const recovery = workflow
    ? await recoveryReadiness(
        items,
        id,
        engine,
        useS3(),
        config.s3EvidenceBucket,
      )
    : null;
  const stepCount = recovery?.stepCount ?? countExecutionSteps(items, engine);
  const readiness = readinessOf({
    state: workflow
      ? recovery?.ready || executions.length
        ? 'COMPLETED'
        : state
      : state,
    stepCount,
    hasExecution: executions.length > 0,
  });

  return {
    rcaId: id,
    workflow,
    activeEngine: workflow ? engine : '',
    requestedEngine: engineFilter || engine,
    engineHandoff: Boolean(workflow && engineFilter && engineFilter !== engine),
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
    executionEngine: execution?.engine ?? '',
    executionStateLabel: execution?.stateLabel ?? '',
    executionId: execution?.executionId ?? '',
    executionAttempts: executions.length,
    executionBlockedCount: execution?.blockedCount ?? 0,
    retrospectiveStatus: execution?.retrospectiveStatus ?? '',
  };
});
