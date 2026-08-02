import {
  GetCommand,
  QueryCommand,
  UpdateCommand,
  type QueryCommandInput,
} from '@aws-sdk/lib-dynamodb';

const OPEN_HYPO_STATES = new Set(['PENDING', 'NEEDS_INVESTIGATION']);

async function findSessionKey(
  ddb: ReturnType<typeof useDynamoDB>,
  tableName: string,
  rcaId: string,
  engine: string,
): Promise<string | null> {
  for (const sessionKey of sessionSkCandidates(engine)) {
    const result = await ddb.send(
      new GetCommand({
        TableName: tableName,
        Key: { PK: rcaPk(rcaId), SK: sessionKey },
        ProjectionExpression: 'PK',
      }),
    );
    if (result.Item) return sessionKey;
  }

  return null;
}

async function closeOpenHypotheses(
  ddb: ReturnType<typeof useDynamoDB>,
  tableName: string,
  rcaId: string,
  engine: string,
  sessionKey: string,
  now: string,
) {
  const prefix = hypothesisSkPrefix(engine, sessionKey);
  const items = [];
  let exclusiveStartKey: QueryCommandInput['ExclusiveStartKey'];

  do {
    const result = await ddb.send(
      new QueryCommand({
        TableName: tableName,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
        ExpressionAttributeValues: { ':pk': rcaPk(rcaId), ':prefix': prefix },
        ProjectionExpression: 'PK, SK, #st',
        ExpressionAttributeNames: { '#st': 'status' },
        ExclusiveStartKey: exclusiveStartKey,
      }),
    );
    items.push(...(result.Items ?? []));
    exclusiveStartKey = result.LastEvaluatedKey;
  } while (exclusiveStartKey);

  const updates = items
    .filter((item) => OPEN_HYPO_STATES.has(item.status))
    .map((item) =>
      ddb.send(
        new UpdateCommand({
          TableName: tableName,
          Key: { PK: item.PK, SK: item.SK },
          UpdateExpression:
            'SET #st = :closed, judgment_reasoning = :reason, updated_at = :now',
          ExpressionAttributeNames: { '#st': 'status' },
          ExpressionAttributeValues: {
            ':closed': 'CLOSED',
            ':reason': '관리자에 의한 분석 중단',
            ':now': now,
          },
        }),
      ),
    );

  await Promise.allSettled(updates);
  return updates.length;
}

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id');
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing session id' });
  }

  const query = getQuery(event);
  const engine = typeof query.engine === 'string' ? query.engine : '';
  if (!isAllowedEngine(engine)) {
    throw createError({
      statusCode: 400,
      statusMessage: 'Missing or invalid engine',
    });
  }

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();
  const sessionKey = await findSessionKey(
    ddb,
    config.dynamodbTableName,
    id,
    engine,
  );
  if (!sessionKey) {
    throw createError({ statusCode: 404, statusMessage: 'Session not found' });
  }

  const now = new Date().toISOString();

  // Cancelling has to fence the running execution, not just relabel the session.
  // The execution holds a claim token and checks it on every state, artifact and
  // trace write, so rotating that token in the same conditional write is what
  // turns the cancel into an actual stop — otherwise the execution keeps writing
  // and finishes side effects that are already inside a lease.
  try {
    await ddb.send(
      new UpdateCommand({
        TableName: config.dynamodbTableName,
        Key: { PK: rcaPk(id), SK: sessionKey },
        ...buildCancelUpdate(
          fencedClaimToken('cancelled'),
          now,
          Math.floor(Date.now() / 1000),
        ),
      }),
    );
  } catch (error) {
    if (isConditionalCheckFailure(error)) {
      // Either the session already reached a terminal state, or a side effect the
      // worker started is still inside its lease. An in-flight external write
      // cannot be cut in half, so the operator retries once the lease lapses.
      throw createError({
        statusCode: 409,
        statusMessage:
          'Session cannot be cancelled — it is already terminal, or a side effect is still holding its lease',
      });
    }
    throw error;
  }

  const closedCount = await closeOpenHypotheses(
    ddb,
    config.dynamodbTableName,
    id,
    engine,
    sessionKey,
    now,
  );

  return { cancelled: true, rcaId: id, engine, closedHypotheses: closedCount };
});
