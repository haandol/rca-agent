import { UpdateCommand } from '@aws-sdk/lib-dynamodb'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing session id' })
  }

  const config = useRuntimeConfig()
  const ddb = useDynamoDB()

  const engines = ['strands', 'cc-headless']
  const now = new Date().toISOString()

  const results = await Promise.allSettled(
    engines.map((engine) =>
      ddb.send(
        new UpdateCommand({
          TableName: config.dynamodbTableName,
          Key: { PK: `RCA#${id}`, SK: `${engine}#SESSION` },
          UpdateExpression: 'SET #st = :cancelled, updated_at = :now',
          ConditionExpression: 'attribute_exists(PK) AND #st <> :completed AND #st <> :failed AND #st <> :cancelled AND #st <> :outdated',
          ExpressionAttributeNames: { '#st': 'state' },
          ExpressionAttributeValues: {
            ':cancelled': 'CANCELLED',
            ':completed': 'COMPLETED',
            ':failed': 'FAILED',
            ':outdated': 'OUTDATED',
            ':now': now,
          },
        }),
      ),
    ),
  )

  // Also try the legacy bare SESSION key
  const legacyResult = await Promise.allSettled([
    ddb.send(
      new UpdateCommand({
        TableName: config.dynamodbTableName,
        Key: { PK: `RCA#${id}`, SK: 'SESSION' },
        UpdateExpression: 'SET #st = :cancelled, updated_at = :now',
        ConditionExpression: 'attribute_exists(PK) AND #st <> :completed AND #st <> :failed AND #st <> :cancelled AND #st <> :outdated',
        ExpressionAttributeNames: { '#st': 'state' },
        ExpressionAttributeValues: {
          ':cancelled': 'CANCELLED',
          ':completed': 'COMPLETED',
          ':failed': 'FAILED',
          ':outdated': 'OUTDATED',
          ':now': now,
        },
      }),
    ),
  ])

  const allResults = [...results, ...legacyResult]
  const succeeded = allResults.filter((r) => r.status === 'fulfilled').length

  // Re-throw unexpected errors
  for (const r of allResults) {
    if (r.status === 'rejected' && (r.reason as { name?: string })?.name !== 'ConditionalCheckFailedException') {
      throw r.reason
    }
  }

  if (succeeded === 0) {
    throw createError({ statusCode: 409, statusMessage: 'Session cannot be cancelled (already terminal or not found)' })
  }

  return { cancelled: true, rcaId: id }
})
