import { ScanCommand } from '@aws-sdk/lib-dynamodb'

export default defineEventHandler(async () => {
  const config = useRuntimeConfig()
  const ddb = useDynamoDB()

  const result = await ddb.send(
    new ScanCommand({
      TableName: config.dynamodbTableName,
      FilterExpression: 'SK = :sk AND begins_with(PK, :prefix)',
      ExpressionAttributeValues: { ':sk': 'SESSION', ':prefix': 'RCA#' },
    }),
  )

  const sessions = (result.Items ?? [])
    .map((item) => ({
      rcaId: (item.PK as string).replace('RCA#', ''),
      state: (item.state as string) || 'UNKNOWN',
      alarmName: (item.alarm_name as string) || 'N/A',
      alarmArn: (item.alarm_arn as string) || '',
      rootCause: (item.root_cause as string) || '',
      confirmed: (item.confirmed as boolean) ?? false,
      errorReason: (item.error_reason as string) || '',
      createdAt: (item.created_at as string) || '',
      updatedAt: (item.updated_at as string) || '',
      engine: (item.engine as string) || 'strands',
    }))
    .sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''))

  return sessions
})
