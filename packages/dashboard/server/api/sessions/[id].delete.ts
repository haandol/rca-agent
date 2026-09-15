import {
  QueryCommand,
  BatchWriteCommand,
  GetCommand,
  TransactWriteCommand,
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
 *
 * The claim and its canonical publication checks commit together. Pending
 * knowledge publication keeps both the source RCA and proposal RCA available;
 * a successful deletion claim makes later proposal source checks fail.
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
      KeyConditionExpression: 'PK = :pk',
      ExpressionAttributeValues: { ':pk': rcaPk(id) },
      ProjectionExpression: 'PK, SK, engine, playbook_id',
    }),
  );

  const items = (result.Items ?? []).filter((item) => {
    if (!engine) return true;
    const sortKey = (item.SK as string) || '';
    if (isSessionSortKey(sortKey)) {
      return ((item.engine as string) || parseEngine(sortKey)) === engine;
    }
    if (sortKey.startsWith('EXEC#') || sortKey === 'EXEC_ACTIVE') return false;
    if (sortKey.startsWith(`${engine}#`)) return true;
    return (
      engine === 'strands' &&
      (sortKey.startsWith('SPAN#') || sortKey.startsWith('HYPO#'))
    );
  });
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

  // Fence every session in scope before deleting anything.
  const sessionKeys = items
    .map((item) => item.SK as string)
    .filter((sortKey) => isSessionSortKey(sortKey));

  const now = new Date().toISOString();
  const nowEpoch = Math.floor(Date.now() / 1000);

  for (const sessionKey of sessionKeys) {
    const found = await ddb.send(
      new GetCommand({
        TableName: config.dynamodbTableName,
        Key: { PK: rcaPk(id), SK: sessionKey },
        ConsistentRead: true,
      }),
    );
    const session = found.Item;
    if (!session)
      throw createError({
        statusCode: 404,
        statusMessage: '이미 삭제된 세션입니다.',
      });
    const claim = buildDeleteClaimUpdate(
      fencedClaimToken('deleted'),
      now,
      nowEpoch,
    );
    // Bind target selection to the same source bytes the apply transaction reads.
    for (const [index, field] of [
      'playbook_id',
      'playbook',
      'completion_playbook',
    ].entries()) {
      const name = `#source${index}`;
      claim.ExpressionAttributeNames[name] = field;
      if (session[field] !== undefined) {
        const value = `:source${index}`;
        claim.ExpressionAttributeValues[value] = session[field];
        claim.ConditionExpression += ` AND ${name} = ${value}`;
      } else {
        claim.ConditionExpression += ` AND attribute_not_exists(${name})`;
      }
    }
    const playbookIds = deletionPlaybookIds(session, items);
    try {
      await ddb.send(
        new TransactWriteCommand({
          TransactItems: [
            {
              Update: {
                TableName: config.dynamodbTableName,
                Key: { PK: rcaPk(id), SK: sessionKey },
                ...claim,
              },
            },
            ...playbookIds.flatMap((playbookId) =>
              ['PLAYBOOK_LIBRARY', 'PLAYBOOK_LIBRARY_STATE'].map(
                (partition) => ({
                  ConditionCheck: {
                    TableName: config.dynamodbTableName,
                    Key: { PK: partition, SK: playbookId },
                    ConditionExpression:
                      'attribute_not_exists(PK) OR attribute_not_exists(publication_status) OR publication_status <> :pending OR ' +
                      '((attribute_not_exists(source_rca_id) OR source_rca_id <> :rca) AND ' +
                      '(attribute_not_exists(proposal_rca_id) OR proposal_rca_id <> :rca))',
                    ExpressionAttributeValues: {
                      ':pending': 'PENDING',
                      ':rca': id,
                    },
                  },
                }),
              ),
            ),
          ],
        }),
      );
    } catch (error) {
      if (
        isConditionalCheckFailure(error) ||
        ((error as { name?: string; CancellationReasons?: { Code?: string }[] })
          ?.name === 'TransactionCanceledException' &&
          (
            error as { CancellationReasons?: { Code?: string }[] }
          ).CancellationReasons?.some(
            (reason) => reason.Code === 'ConditionalCheckFailed',
          ))
      ) {
        // Refusing here is the point: an operator has to cancel an active
        // session first, which fences the execution, and only then delete.
        throw createError({
          statusCode: 409,
          statusMessage:
            '분석·실행·게시가 진행 중이거나 삭제 기준이 변경되었습니다. 진행 중인 작업이 끝난 뒤 다시 삭제하세요.',
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
      ProjectionExpression: 'SK, engine',
    }),
  );
  const survivingEngines = new Set(
    (remaining.Items ?? [])
      .filter((item) => isSessionSortKey((item.SK as string) || ''))
      .map(
        (item) =>
          (item.engine as string) || parseEngine((item.SK as string) || ''),
      )
      .filter(Boolean),
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
 * Protect publication owned by this session without blocking unrelated pending heads.
 * Include the fixed proposal target because it can differ from the incident's playbook.
 */
function deletionPlaybookIds(
  session: Record<string, unknown>,
  items: Record<string, unknown>[],
): string[] {
  const ids = new Set<string>();
  if (typeof session.playbook_id === 'string' && session.playbook_id)
    ids.add(session.playbook_id);
  const raw = session.completion_playbook ?? session.playbook;
  try {
    const book = typeof raw === 'string' ? JSON.parse(raw) : raw;
    const target = book?.comparison?.proposal?.playbook_id;
    if (typeof target === 'string' && target) ids.add(target);
    const selected = book?.comparison?.selected_playbook_id;
    if (typeof selected === 'string' && selected) ids.add(selected);
  } catch {
    /* A malformed original cannot create a new proposal; retain stored decision targets. */
  }
  const owner = (session.engine as string) || parseEngine(session.SK as string);
  for (const item of items) {
    if (
      (item.SK as string).startsWith(`${owner}#PLAYBOOK_PROPOSAL#`) &&
      typeof item.playbook_id === 'string' &&
      item.playbook_id
    )
      ids.add(item.playbook_id);
  }
  return [...ids];
}

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

/** Keep execution authority intact by refusing deletion while any attempt is still active. */
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
