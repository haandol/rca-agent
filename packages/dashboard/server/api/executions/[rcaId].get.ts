import { QueryCommand } from '@aws-sdk/lib-dynamodb';

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

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
      ExpressionAttributeValues: { ':pk': `RCA#${rcaId}`, ':prefix': 'EXEC#' },
    }),
  );

  const executions = (result.Items ?? []).map(readExecution).sort((a, b) => {
    if (a.attempt !== b.attempt) return b.attempt - a.attempt;
    return (b.updatedAt || '').localeCompare(a.updatedAt || '');
  });

  return { rcaId, executions };
});
