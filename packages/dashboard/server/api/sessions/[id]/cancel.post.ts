import { UpdateCommand } from '@aws-sdk/lib-dynamodb'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing session id' })
  }

  const config = useRuntimeConfig()
  const ddb = useDynamoDB()

  try {
    await ddb.send(
      new UpdateCommand({
        TableName: config.dynamodbTableName,
        Key: { PK: `RCA#${id}`, SK: 'SESSION' },
        UpdateExpression: 'SET #st = :cancelled, updated_at = :now',
        ConditionExpression: 'attribute_exists(PK) AND #st NOT IN (:completed, :failed, :cancelled, :outdated)',
        ExpressionAttributeNames: { '#st': 'state' },
        ExpressionAttributeValues: {
          ':cancelled': 'CANCELLED',
          ':completed': 'COMPLETED',
          ':failed': 'FAILED',
          ':outdated': 'OUTDATED',
          ':now': new Date().toISOString(),
        },
      }),
    )
  } catch (e: unknown) {
    const err = e as { name?: string }
    if (err.name === 'ConditionalCheckFailedException') {
      throw createError({ statusCode: 409, statusMessage: 'Session cannot be cancelled (already terminal or not found)' })
    }
    throw e
  }

  return { cancelled: true, rcaId: id }
})
