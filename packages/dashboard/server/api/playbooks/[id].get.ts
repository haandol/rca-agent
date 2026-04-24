import { QueryCommand } from '@aws-sdk/lib-dynamodb'

function parseEngine(sk: string): string {
  if (sk === 'SESSION' || sk.startsWith('SPAN#') || sk.startsWith('HYPO#')) return 'strands'
  return sk.split('#')[0] ?? 'strands'
}

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing RCA id' })
  }

  const query = getQuery(event)
  const engineFilter = (query.engine as string) || ''

  const config = useRuntimeConfig()
  const ddb = useDynamoDB()

  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk',
      ExpressionAttributeValues: { ':pk': `RCA#${id}` },
    }),
  )

  const items = result.Items ?? []

  const span = items.find((i) => {
    const sk = (i.SK as string) || ''
    const isSpan = sk.includes('#SPAN#') || sk.startsWith('SPAN#')
    if (!isSpan) return false
    if (i.span_type !== 'PLAYBOOK') return false
    if (!engineFilter) return true
    return parseEngine(sk) === engineFilter
  })

  if (!span) {
    throw createError({ statusCode: 404, statusMessage: 'Playbook not found' })
  }

  const metadata = (span.metadata as Record<string, unknown>) || {}

  return {
    rcaId: id,
    spanStatus: (span.span_status as string) || 'UNKNOWN',
    durationMs: (span.duration_ms as number) ?? null,
    error: (span.error as string) || null,
    outputSummary: (span.output_summary as string) || '',
    playbook_id: (metadata.playbook_id as string) || '',
    failure_type: (metadata.failure_type as string) || '',
    symptom_pattern: (metadata.symptom_pattern as string) || '',
    verification_steps: (metadata.verification_steps as string[]) || [],
    temporary_mitigation: (metadata.temporary_mitigation as string) || '',
    permanent_remediation: (metadata.permanent_remediation as string) || '',
    prevention_measures: (metadata.prevention_measures as string[]) || [],
    tags: (metadata.tags as string[]) || [],
  }
})
