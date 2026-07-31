import {
  QueryCommand,
  BatchWriteCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import { DeleteObjectCommand } from '@aws-sdk/client-s3';

/**
 * Deletes a session's records — only once nothing is still running against them.
 *
 * Deleting a live session removes the record the fencing depends on: the running
 * execution never learns its claim is gone and keeps writing, while a redelivered
 * SQS message finds no session and starts the analysis over, so both run at once.
 * So every session record in scope is fenced first, and a session that is still
 * active makes the whole request fail rather than deleting part of it.
 */
export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id');
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing session id' });
  }

  const query = getQuery(event);
  const engine = typeof query.engine === 'string' ? query.engine : undefined;

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();
  const s3 = useS3();

  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: engine
        ? 'PK = :pk AND begins_with(SK, :skPrefix)'
        : 'PK = :pk',
      ExpressionAttributeValues: engine
        ? { ':pk': `RCA#${id}`, ':skPrefix': `${engine}#` }
        : { ':pk': `RCA#${id}` },
      ProjectionExpression: 'PK, SK',
    }),
  );

  const items = result.Items ?? [];
  if (!items.length) {
    throw createError({ statusCode: 404, statusMessage: 'Session not found' });
  }

  // An execution is a separate lifecycle from the analysis, but its records live
  // in the same partition and this delete would take them too — and the running
  // execution checks its own claim on every write. So a live execution blocks the
  // delete for the same reason a live analysis does.
  const running = await inFlightExecutions(ddb, config, id);
  if (running.length) {
    throw createError({
      statusCode: 409,
      statusMessage: `An execution is ${running[0]!.stateLabel} for this report — wait for it to finish before deleting`,
    });
  }

  // Fence every session in scope before deleting anything. Deleting without an
  // engine filter covers both engines, and each one has its own claim.
  const sessionKeys = items
    .map((item) => item.SK as string)
    .filter((sortKey) => isSessionSortKey(sortKey));

  const now = new Date().toISOString();
  const nowEpoch = Math.floor(Date.now() / 1000);

  for (const sessionKey of sessionKeys) {
    try {
      await ddb.send(
        new UpdateCommand({
          TableName: config.dynamodbTableName,
          Key: { PK: `RCA#${id}`, SK: sessionKey },
          ...buildDeleteClaimUpdate(fencedClaimToken('deleted'), now, nowEpoch),
        }),
      );
    } catch (error) {
      if (isConditionalCheckFailure(error)) {
        // Refusing here is the point: an operator has to cancel an active
        // session first, which fences the execution, and only then delete.
        throw createError({
          statusCode: 409,
          statusMessage: `Session ${sessionKey} is still active — cancel it first, then delete`,
        });
      }
      throw error;
    }
  }

  const chunks = [];
  for (let i = 0; i < items.length; i += 25) {
    chunks.push(items.slice(i, i + 25));
  }

  for (const chunk of chunks) {
    await ddb.send(
      new BatchWriteCommand({
        RequestItems: {
          [config.dynamodbTableName]: chunk.map((item) => ({
            DeleteRequest: { Key: { PK: item.PK, SK: item.SK } },
          })),
        },
      }),
    );
  }

  let hasRemainingSession = false;
  if (engine) {
    const remaining = await ddb.send(
      new QueryCommand({
        TableName: config.dynamodbTableName,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': `RCA#${id}` },
        Select: 'COUNT',
      }),
    );
    hasRemainingSession = (remaining.Count ?? 0) > 0;
  }

  if (!hasRemainingSession) {
    try {
      await s3.send(
        new DeleteObjectCommand({
          Bucket: config.s3ReportBucket,
          Key: `reports/${id}.md`,
        }),
      );
    } catch (_) {
      // S3 리포트가 없어도 무시
    }
  }

  return { deleted: true, rcaId: id, engine, itemCount: items.length };
});

async function inFlightExecutions(
  ddb: ReturnType<typeof useDynamoDB>,
  config: ReturnType<typeof useRuntimeConfig>,
  rcaId: string,
): Promise<ExecutionSummary[]> {
  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
      ExpressionAttributeValues: { ':pk': `RCA#${rcaId}`, ':prefix': 'EXEC#' },
    }),
  );
  return (result.Items ?? [])
    .map(readExecution)
    .filter((execution) => !isTerminalExecution(execution.state));
}
