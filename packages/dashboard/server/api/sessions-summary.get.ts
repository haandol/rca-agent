import {
  GetCommand,
  QueryCommand,
  type QueryCommandInput,
} from '@aws-sdk/lib-dynamodb';

/**
 * What the archive contains, counted across all of it.
 *
 * These counts are deliberately not part of a list page. '승인 대기 10건' becoming
 * '3건' because only the first page was counted is worse than a slow number — a
 * person would read the wrong amount of work left. So the list pages and the
 * totals are separate reads, and only the totals walk the whole index.
 *
 * Walking the index is cheap in the way that matters: it holds session records
 * only, so this reads roughly one item per session rather than every span and
 * hypothesis in the table.
 *
 * Enrich completed legacy analyses and registered three-part workflows. Early
 * recovery can await approval while its parent analysis is still running.
 *
 * It sits beside the session collection rather than inside it: a path under
 * `sessions/` would collide with a session whose id happened to be the same word,
 * making that session unreachable.
 */
export default defineEventHandler(async () => {
  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const indexedKeys = new Map<string, { PK: string; SK: string }>();

  for (const engine of ALLOWED_ENGINES) {
    let startKey: QueryCommandInput['ExclusiveStartKey'];
    do {
      const result = await ddb.send(
        new QueryCommand({
          TableName: config.dynamodbTableName,
          IndexName: SESSION_LIST_INDEX,
          KeyConditionExpression: '#pk = :engine',
          // The INCLUDE index discovers keys only. Workflow authority lives in the base table.
          ExpressionAttributeNames: {
            '#pk': LIST_PARTITION_KEY,
          },
          ExpressionAttributeValues: { ':engine': engine },
          ProjectionExpression: 'PK, SK',
          ExclusiveStartKey: startKey,
        }),
      );
      for (const item of result.Items ?? []) {
        if (
          typeof item.PK === 'string' &&
          typeof item.SK === 'string' &&
          isSessionSortKey(item.SK)
        )
          indexedKeys.set(`${item.PK}\0${item.SK}`, {
            PK: item.PK,
            SK: item.SK,
          });
      }
      startKey = result.LastEvaluatedKey;
    } while (startKey);
  }

  // A GSI can lag an ownership change or deletion; never infer workflow from its projection.
  const baseSessions = await Promise.all(
    [...indexedKeys.values()].map(async (Key) => {
      const response = await ddb.send(
        new GetCommand({
          TableName: config.dynamodbTableName,
          Key,
          ConsistentRead: true,
          ProjectionExpression: 'PK, SK, engine, #st, workflow',
          ExpressionAttributeNames: { '#st': 'state' },
        }),
      );
      const item = response.Item;
      if (!item || item.PK !== Key.PK || item.SK !== Key.SK) return null;
      const owner = (item.engine as string) || parseEngine(Key.SK);
      if (!isAllowedEngine(owner)) return null;
      return {
        rcaId: rcaIdFromPk(Key.PK),
        engine: owner,
        state: (item.state as string) || 'UNKNOWN',
        workflow: (item.workflow as string) || '',
      };
    }),
  );
  const sessions = baseSessions.filter(
    (session): session is NonNullable<typeof session> => session !== null,
  );

  const completed = sessions.filter(
    (session) =>
      session.state === 'COMPLETED' || session.workflow === 'recovery-first-v1',
  );

  const readinessOfCompleted = await Promise.all(
    completed.map(async (session) => {
      const items = await readAnalysisPartition(
        ddb,
        config.dynamodbTableName,
        session.rcaId,
      );
      const executions = items
        .filter((entry) => isExecutionItem((entry.SK as string) || ''))
        .map(readExecution)
        .filter((execution) =>
          hasAnalysisParts(items, session.engine)
            ? execution.rcaId === session.rcaId &&
              isAllowedEngine(execution.engine)
            : execution.engine === session.engine,
        );

      const recovery = hasAnalysisParts(items, session.engine)
        ? await recoveryReadiness(
            items,
            session.rcaId,
            session.engine,
            useS3(),
            config.s3EvidenceBucket,
          )
        : null;
      return {
        workflow: session.workflow,
        engine: session.engine,
        state: session.state,
        readiness: readinessOf({
          state:
            recovery?.ready || (recovery && executions.length)
              ? 'COMPLETED'
              : session.state,
          stepCount:
            recovery?.stepCount ?? countExecutionSteps(items, session.engine),
          hasExecution: executions.length > 0,
        }),
        executionState:
          latestExecution(executions, hasAnalysisParts(items, session.engine))
            ?.state ?? '',
      };
    }),
  );

  const byReadiness: Record<string, number> = {};
  for (const entry of readinessOfCompleted) {
    byReadiness[entry.readiness] = (byReadiness[entry.readiness] ?? 0) + 1;
  }

  const byState: Record<string, number> = {};
  for (const session of sessions) {
    byState[session.state] = (byState[session.state] ?? 0) + 1;
  }

  return {
    total: sessions.length,
    byState,
    byReadiness,
    // The outcome each completed session resolves to, so the client can tally the
    // one word it shows without re-deriving it from two lifecycles.
    completedOutcomes: readinessOfCompleted.map((entry) => ({
      state: entry.state,
      workflow: entry.workflow,
      readiness: entry.readiness,
      executionState: entry.executionState,
    })),
  };
});
