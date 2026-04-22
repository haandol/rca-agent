import {
  DynamoDBClient,
  PutItemCommand,
  UpdateItemCommand,
  GetItemCommand,
} from '@aws-sdk/client-dynamodb';

const TABLE_NAME = process.env.DYNAMODB_TABLE_NAME ?? '';
const SESSION_TTL_DAYS = 90;
const ENGINE = 'cc-headless';

const ddb = new DynamoDBClient({});

export async function checkDuplicate(
  idempotencyKey: string,
): Promise<boolean> {
  if (!TABLE_NAME) return false;

  const result = await ddb.send(
    new GetItemCommand({
      TableName: TABLE_NAME,
      Key: {
        PK: { S: `IDEMP#${idempotencyKey}` },
        SK: { S: 'SESSION' },
      },
    }),
  );

  return !!result.Item;
}

export async function createSession(
  rcaId: string,
  alarmName: string,
  idempotencyKey: string,
): Promise<boolean> {
  if (!TABLE_NAME) return false;

  const now = new Date().toISOString();
  const ttl = Math.floor(Date.now() / 1000) + SESSION_TTL_DAYS * 86400;

  try {
    await ddb.send(
      new PutItemCommand({
        TableName: TABLE_NAME,
        Item: {
          PK: { S: `RCA#${rcaId}` },
          SK: { S: 'SESSION' },
          rca_id: { S: rcaId },
          idempotency_key: { S: idempotencyKey },
          alarm_name: { S: alarmName },
          state: { S: 'ALARM_RECEIVED' },
          engine: { S: ENGINE },
          created_at: { S: now },
          updated_at: { S: now },
          ttl: { N: String(ttl) },
        },
        ConditionExpression: 'attribute_not_exists(PK)',
      }),
    );

    await ddb.send(
      new PutItemCommand({
        TableName: TABLE_NAME,
        Item: {
          PK: { S: `IDEMP#${idempotencyKey}` },
          SK: { S: 'SESSION' },
          rca_id: { S: rcaId },
          ttl: { N: String(ttl) },
        },
        ConditionExpression: 'attribute_not_exists(PK)',
      }),
    );

    return true;
  } catch (err: unknown) {
    if ((err as { name?: string }).name === 'ConditionalCheckFailedException') {
      return false;
    }
    throw err;
  }
}

export async function updateState(
  rcaId: string,
  state: string,
): Promise<void> {
  if (!TABLE_NAME) return;

  await ddb.send(
    new UpdateItemCommand({
      TableName: TABLE_NAME,
      Key: {
        PK: { S: `RCA#${rcaId}` },
        SK: { S: 'SESSION' },
      },
      UpdateExpression: 'SET #state = :state, updated_at = :now',
      ExpressionAttributeNames: { '#state': 'state' },
      ExpressionAttributeValues: {
        ':state': { S: state },
        ':now': { S: new Date().toISOString() },
      },
    }),
  );
}

export async function markCompleted(
  rcaId: string,
  rootCause: string,
): Promise<void> {
  if (!TABLE_NAME) return;

  await ddb.send(
    new UpdateItemCommand({
      TableName: TABLE_NAME,
      Key: {
        PK: { S: `RCA#${rcaId}` },
        SK: { S: 'SESSION' },
      },
      UpdateExpression:
        'SET #state = :state, root_cause = :rc, updated_at = :now',
      ExpressionAttributeNames: { '#state': 'state' },
      ExpressionAttributeValues: {
        ':state': { S: 'COMPLETED' },
        ':rc': { S: rootCause },
        ':now': { S: new Date().toISOString() },
      },
    }),
  );
}

export async function markFailed(
  rcaId: string,
  errorReason: string,
): Promise<void> {
  if (!TABLE_NAME) return;

  await ddb.send(
    new UpdateItemCommand({
      TableName: TABLE_NAME,
      Key: {
        PK: { S: `RCA#${rcaId}` },
        SK: { S: 'SESSION' },
      },
      UpdateExpression:
        'SET #state = :state, error_reason = :err, updated_at = :now',
      ExpressionAttributeNames: { '#state': 'state' },
      ExpressionAttributeValues: {
        ':state': { S: 'FAILED' },
        ':err': { S: errorReason },
        ':now': { S: new Date().toISOString() },
      },
    }),
  );
}
