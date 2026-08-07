import { QueryCommand, type QueryCommandInput } from '@aws-sdk/lib-dynamodb';

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

  const items: Record<string, unknown>[] = [];
  let startKey: QueryCommandInput['ExclusiveStartKey'];
  do {
    const result = await ddb.send(
      new QueryCommand({
        TableName: config.dynamodbTableName,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
        ExpressionAttributeValues: {
          ':pk': rcaPk(rcaId),
          ':prefix': EXECUTION_SK_PREFIX,
        },
        ExclusiveStartKey: startKey,
      }),
    );
    items.push(...(result.Items ?? []));
    startKey = result.LastEvaluatedKey;
  } while (startKey);

  const executions = items
    .map(readExecution)
    .filter((execution) => execution.engine === engine)
    .sort((a, b) => {
      if (a.attempt !== b.attempt) return b.attempt - a.attempt;
      return (b.updatedAt || '').localeCompare(a.updatedAt || '');
    });

  return { rcaId, engine, executions };
});
