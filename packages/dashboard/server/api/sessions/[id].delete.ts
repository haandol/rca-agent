import { QueryCommand, BatchWriteCommand } from '@aws-sdk/lib-dynamodb'
import { DeleteObjectCommand } from '@aws-sdk/client-s3'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing session id' })
  }

  const query = getQuery(event)
  const engine = typeof query.engine === 'string' ? query.engine : undefined

  const config = useRuntimeConfig()
  const ddb = useDynamoDB()
  const s3 = useS3()

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
  )

  const items = result.Items ?? []
  if (!items.length) {
    throw createError({ statusCode: 404, statusMessage: 'Session not found' })
  }

  const chunks = []
  for (let i = 0; i < items.length; i += 25) {
    chunks.push(items.slice(i, i + 25))
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
    )
  }

  let hasRemainingSession = false
  if (engine) {
    const remaining = await ddb.send(
      new QueryCommand({
        TableName: config.dynamodbTableName,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': `RCA#${id}` },
        Select: 'COUNT',
      }),
    )
    hasRemainingSession = (remaining.Count ?? 0) > 0
  }

  if (!hasRemainingSession) {
    try {
      await s3.send(
        new DeleteObjectCommand({
          Bucket: config.s3ReportBucket,
          Key: `reports/${id}.md`,
        }),
      )
    } catch (_) {
      // S3 리포트가 없어도 무시
    }
  }

  return { deleted: true, rcaId: id, engine, itemCount: items.length }
})
