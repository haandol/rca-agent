import {
  GetCommand,
  QueryCommand,
  UpdateCommand,
  type QueryCommandInput,
} from '@aws-sdk/lib-dynamodb';

const ALLOWED_ENGINES = new Set(['strands', 'cc-headless']);
const OPEN_HYPO_STATES = new Set(['PENDING', 'NEEDS_INVESTIGATION']);

async function findSessionKey(
  ddb: ReturnType<typeof useDynamoDB>,
  tableName: string,
  rcaId: string,
  engine: string,
): Promise<string | null> {
  const sessionKeys =
    engine === 'strands'
      ? ['strands#SESSION', 'SESSION']
      : [`${engine}#SESSION`];

  for (const sessionKey of sessionKeys) {
    const result = await ddb.send(
      new GetCommand({
        TableName: tableName,
        Key: { PK: `RCA#${rcaId}`, SK: sessionKey },
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
  const prefix = sessionKey === 'SESSION' ? 'HYPO#' : `${engine}#HYPO#`;
  const items = [];
  let exclusiveStartKey: QueryCommandInput['ExclusiveStartKey'];

  do {
    const result = await ddb.send(
      new QueryCommand({
        TableName: tableName,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
        ExpressionAttributeValues: { ':pk': `RCA#${rcaId}`, ':prefix': prefix },
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
  if (!ALLOWED_ENGINES.has(engine)) {
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

  try {
    await ddb.send(
      new UpdateCommand({
        TableName: config.dynamodbTableName,
        Key: { PK: `RCA#${id}`, SK: sessionKey },
        UpdateExpression: 'SET #st = :cancelled, updated_at = :now',
        ConditionExpression:
          'attribute_exists(PK) AND #st <> :completed AND #st <> :failed AND #st <> :cancelled AND #st <> :outdated',
        ExpressionAttributeNames: { '#st': 'state' },
        ExpressionAttributeValues: {
          ':cancelled': 'CANCELLED',
          ':completed': 'COMPLETED',
          ':failed': 'FAILED',
          ':outdated': 'OUTDATED',
          ':now': now,
        },
      }),
    );
  } catch (error) {
    if (
      (error as { name?: string })?.name === 'ConditionalCheckFailedException'
    ) {
      throw createError({
        statusCode: 409,
        statusMessage:
          'Session cannot be cancelled (already terminal or not found)',
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
