import { QueryCommand, type QueryCommandInput } from '@aws-sdk/lib-dynamodb';

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
 * Enrichment is limited to what can change an answer. Only a COMPLETED analysis
 * can be waiting on an approval, so the partitions of the rest are never opened.
 *
 * It sits beside the session collection rather than inside it: a path under
 * `sessions/` would collide with a session whose id happened to be the same word,
 * making that session unreachable.
 */
export default defineEventHandler(async () => {
  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const sessions: { rcaId: string; engine: string; state: string }[] = [];

  for (const engine of ALLOWED_ENGINES) {
    let startKey: QueryCommandInput['ExclusiveStartKey'];
    do {
      const result = await ddb.send(
        new QueryCommand({
          TableName: config.dynamodbTableName,
          IndexName: SESSION_LIST_INDEX,
          KeyConditionExpression: '#pk = :engine',
          ExpressionAttributeNames: { '#pk': LIST_PARTITION_KEY },
          ExpressionAttributeValues: { ':engine': engine },
          ProjectionExpression: 'PK, #st, engine',
          ExclusiveStartKey: startKey,
        }),
      );
      for (const item of result.Items ?? []) {
        sessions.push({
          rcaId: rcaIdFromPk(item.PK as string),
          engine: (item.engine as string) || engine,
          state: (item.state as string) || 'UNKNOWN',
        });
      }
      startKey = result.LastEvaluatedKey;
    } while (startKey);
  }

  const completed = sessions.filter((session) => session.state === 'COMPLETED');

  const readinessOfCompleted = await Promise.all(
    completed.map(async (session) => {
      const partition = await ddb.send(
        new QueryCommand({
          TableName: config.dynamodbTableName,
          KeyConditionExpression: 'PK = :pk',
          ExpressionAttributeValues: { ':pk': rcaPk(session.rcaId) },
        }),
      );
      const items = partition.Items ?? [];
      const executions = items
        .filter((entry) => isExecutionItem((entry.SK as string) || ''))
        .map(readExecution)
        .filter(
          (execution) =>
            !execution.engine || execution.engine === session.engine,
        );

      return {
        engine: session.engine,
        state: session.state,
        readiness: readinessOf({
          state: session.state,
          stepCount: countExecutionSteps(items, session.engine),
          hasExecution: executions.length > 0,
        }),
        executionState: latestExecution(executions)?.state ?? '',
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
      readiness: entry.readiness,
      executionState: entry.executionState,
    })),
  };
});
