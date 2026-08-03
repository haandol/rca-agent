import {
  QueryCommand,
  BatchWriteCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import { DeleteObjectsCommand, ListObjectsV2Command } from '@aws-sdk/client-s3';

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
        ? { ':pk': rcaPk(id), ':skPrefix': `${engine}#` }
        : { ':pk': rcaPk(id) },
      ProjectionExpression: 'PK, SK',
    }),
  );

  const items = result.Items ?? [];
  if (!items.length) {
    throw createError({
      statusCode: 404,
      statusMessage: '이미 삭제된 세션입니다.',
    });
  }

  // An execution is a separate lifecycle from the analysis, but its records live
  // in the same partition and this delete would take them too — and the running
  // execution checks its own claim on every write. So a live execution blocks the
  // delete for the same reason a live analysis does.
  const running = await inFlightExecutions(ddb, config, id);
  if (running.length) {
    throw createError({
      statusCode: 409,
      statusMessage: `실행이 끝나지 않았습니다(${running[0]!.stateLabel}). 실행이 끝난 뒤에 삭제할 수 있습니다.`,
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
          Key: { PK: rcaPk(id), SK: sessionKey },
          ...buildDeleteClaimUpdate(fencedClaimToken('deleted'), now, nowEpoch),
        }),
      );
    } catch (error) {
      if (isConditionalCheckFailure(error)) {
        // Refusing here is the point: an operator has to cancel an active
        // session first, which fences the execution, and only then delete.
        throw createError({
          statusCode: 409,
          statusMessage:
            '분석이 아직 진행 중입니다. 먼저 중단한 뒤에 삭제할 수 있습니다.',
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

  /**
   * The artifacts this deletion also removes.
   *
   * Reports are stored per engine, so deleting one engine's session takes only
   * that engine's reports. Evidence is not: both engines analyse the same alarm
   * under one RCA id, so it is removed only once no session for this RCA is left
   * — otherwise deleting one engine's row would strip the evidence the other
   * engine's report still cites.
   */
  const remaining = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk',
      ExpressionAttributeValues: { ':pk': rcaPk(id) },
      ProjectionExpression: 'SK',
    }),
  );
  const survivingEngines = new Set(
    (remaining.Items ?? [])
      .map((item) => (item.SK as string) || '')
      .filter((sortKey) => isSessionSortKey(sortKey))
      .map((sortKey) => parseEngine(sortKey)),
  );

  const prefixes = engine
    ? [`reports/${engine}/${id}/`]
    : ALLOWED_ENGINES.map((name) => `reports/${name}/${id}/`);
  if (!survivingEngines.size) prefixes.push(`rca/${id}/`);

  const deletedObjectCount = await deletePrefixes(
    s3,
    config.s3ReportBucket,
    prefixes,
  );

  return {
    deleted: true,
    rcaId: id,
    engine,
    itemCount: items.length,
    deletedObjectCount,
  };
});

/**
 * Removes every object under the given prefixes.
 *
 * Artifacts are written under a path per RCA, per engine and per attempt, so a
 * session's reports are a prefix rather than one key. Deleting a single key left
 * everything behind, and the objects then sat until the lifecycle rule swept them
 * weeks later — a deleted session kept its evidence readable.
 *
 * Failures here do not fail the request: the records are already gone, and
 * reporting a failure would invite a retry that finds no session and 404s. The
 * count is returned so a caller can see what was actually removed.
 */
async function deletePrefixes(
  s3: ReturnType<typeof useS3>,
  bucket: string,
  prefixes: string[],
): Promise<number> {
  let removed = 0;

  for (const prefix of prefixes) {
    let continuationToken: string | undefined;
    do {
      try {
        const listed = await s3.send(
          new ListObjectsV2Command({
            Bucket: bucket,
            Prefix: prefix,
            ContinuationToken: continuationToken,
          }),
        );
        const keys = (listed.Contents ?? [])
          .map((object) => object.Key)
          .filter((key): key is string => Boolean(key));

        if (keys.length) {
          await s3.send(
            new DeleteObjectsCommand({
              Bucket: bucket,
              Delete: { Objects: keys.map((Key) => ({ Key })), Quiet: true },
            }),
          );
          removed += keys.length;
        }
        continuationToken = listed.IsTruncated
          ? listed.NextContinuationToken
          : undefined;
      } catch {
        // The DynamoDB records are already deleted, so a storage failure must not
        // turn a completed deletion into an error the operator would retry.
        continuationToken = undefined;
      }
    } while (continuationToken);
  }

  return removed;
}

async function inFlightExecutions(
  ddb: ReturnType<typeof useDynamoDB>,
  config: ReturnType<typeof useRuntimeConfig>,
  rcaId: string,
): Promise<ExecutionSummary[]> {
  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
      ExpressionAttributeValues: {
        ':pk': rcaPk(rcaId),
        ':prefix': EXECUTION_SK_PREFIX,
      },
    }),
  );
  return (result.Items ?? [])
    .map(readExecution)
    .filter((execution) => !isTerminalExecution(execution.state));
}
