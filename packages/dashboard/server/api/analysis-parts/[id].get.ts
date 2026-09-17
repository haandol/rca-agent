import { QueryCommand, type QueryCommandInput } from '@aws-sdk/lib-dynamodb';

/** Read only the requested incident; a known new workflow never falls back to legacy artifacts. */
export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id') ?? '';
  const engine = getQuery(event).engine;
  if (
    !/^[A-Za-z0-9_-]+$/.test(id) ||
    typeof engine !== 'string' ||
    !isAllowedEngine(engine)
  )
    throw createError({
      statusCode: 400,
      statusMessage: 'Invalid RCA or engine',
    });
  const config = useRuntimeConfig();
  const items: Record<string, any>[] = [];
  let startKey: QueryCommandInput['ExclusiveStartKey'];
  do {
    const page = await useDynamoDB().send(
      new QueryCommand({
        TableName: config.dynamodbTableName,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': rcaPk(id) },
        ConsistentRead: true,
        ExclusiveStartKey: startKey,
      }),
    );
    items.push(...(page.Items ?? []));
    startKey = page.LastEvaluatedKey;
  } while (startKey);
  if (!hasAnalysisParts(items, engine))
    return {
      rcaId: id,
      engine,
      workflow: null,
      activeEngine: '',
      historicalView: false,
      parentEligible: false,
      parts: [],
    };
  try {
    return await readAnalysisParts(
      items,
      id,
      engine,
      useS3(),
      config.s3EvidenceBucket,
    );
  } catch {
    throw createError({
      statusCode: 409,
      statusMessage: '분석 파트의 요청 식별자를 확인할 수 없습니다',
    });
  }
});
