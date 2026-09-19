/**
 * Every execution attempt against one report, newest attempt first.
 *
 * A report can be executed more than once, and a failed attempt's evidence is
 * kept — it is the only record a person can read to find out why. So this
 * returns the history rather than only the current state.
 */
export default defineEventHandler(async (event) => {
  const rcaId = getRouterParam(event, 'rcaId');
  if (!rcaId) {
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

  const items = await readAnalysisPartition(
    ddb,
    config.dynamodbTableName,
    rcaId,
  );
  // A takeover changes the analysis engine, not the RCA-scoped execution lineage.
  const lineage =
    hasAnalysisParts(items, engine) ||
    items.some(
      (item) =>
        item.SK === ANALYSIS_SESSION_SK &&
        item.workflow === 'recovery-first-v1',
    );
  const active = lineage
    ? items.find(
        (item) => item.SK === ACTIVE_EXECUTION_SK && item.PK === rcaPk(rcaId),
      )
    : undefined;

  const executions = items
    .filter((item) => isExecutionItem(String(item.SK ?? '')))
    .map((item) => ({
      ...readExecution(item),
      ...readPublicationHistory(item, items),
    }))
    .filter((execution) =>
      lineage
        ? execution.rcaId === rcaId && isAllowedEngine(execution.engine)
        : execution.engine === engine,
    )
    .sort((a, b) => {
      if (lineage) {
        const activeStates = ['PENDING_APPROVAL', 'EXECUTING', 'VERIFYING'];
        const difference =
          Number(activeStates.includes(b.state)) -
          Number(activeStates.includes(a.state));
        if (difference) return difference;
      }
      if (a.attempt !== b.attempt) return b.attempt - a.attempt;
      return (b.updatedAt || '').localeCompare(a.updatedAt || '');
    });

  return {
    rcaId,
    engine,
    executionScope: lineage ? 'rca' : 'engine',
    activeExecutionId:
      typeof active?.execution_id === 'string' ? active.execution_id : '',
    activeExecutionEngine:
      typeof active?.engine === 'string' ? active.engine : '',
    executions,
  };
});
