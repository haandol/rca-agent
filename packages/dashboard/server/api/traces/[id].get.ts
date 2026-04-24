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

  const session = items.find((i) => (i.SK as string).endsWith('#SESSION') || i.SK === 'SESSION')
  const sessionData = session
    ? {
        state: (session.state as string) || 'UNKNOWN',
        alarmName: (session.alarm_name as string) || '',
        alarmArn: (session.alarm_arn as string) || '',
        rootCause: (session.root_cause as string) || '',
        confirmed: (session.confirmed as boolean) ?? false,
        errorReason: (session.error_reason as string) || '',
        createdAt: (session.created_at as string) || '',
        updatedAt: (session.updated_at as string) || '',
        engine: (session.engine as string) || parseEngine(session.SK as string),
      }
    : null

  const spans = items
    .filter((i) => {
      const sk = (i.SK as string) || ''
      return sk.includes('#SPAN#') || sk.startsWith('SPAN#')
    })
    .map((i) => {
      const sk = i.SK as string
      const spanId = sk.includes('#SPAN#') ? (sk.split('#SPAN#')[1] ?? '') : sk.replace('SPAN#', '')
      return {
        spanId,
        spanType: (i.span_type as string) || '',
        spanStatus: (i.span_status as string) || '',
        parentSpanId: (i.parent_span_id as string) || null,
        loopIndex: (i.loop_index as number) ?? null,
        startTime: (i.start_time as string) || '',
        endTime: (i.end_time as string) || null,
        durationMs: (i.duration_ms as number) ?? null,
        inputSummary: (i.input_summary as string) || '',
        outputSummary: (i.output_summary as string) || '',
        error: (i.error as string) || null,
        metadata: (i.metadata as Record<string, unknown>) || null,
        engine: (i.engine as string) || parseEngine(sk),
      }
    })
    .sort((a, b) => a.startTime.localeCompare(b.startTime))

  const hypotheses = items
    .filter((i) => {
      const sk = (i.SK as string) || ''
      return sk.includes('#HYPO#') || sk.startsWith('HYPO#')
    })
    .map((i) => {
      const sk = i.SK as string
      const hypothesisId = sk.includes('#HYPO#') ? (sk.split('#HYPO#')[1] ?? '') : sk.replace('HYPO#', '')
      return {
        hypothesisId,
        treeId: (i.tree_id as string) || '',
        parentId: (i.parent_id as string) || null,
        depth: (i.depth as number) ?? 0,
        description: (i.description as string) || '',
        category: (i.category as string) || '',
        confidenceScore: (i.confidence_score as number) ?? 0,
        status: (i.status as string) || 'PENDING',
        requiredEvidence: (i.required_evidence as string[]) || [],
        referencedPlaybookId: (i.referenced_playbook_id as string) || null,
        evidenceSummary: (i.evidence_summary as string) || '',
        judgmentReasoning: (i.judgment_reasoning as string) || '',
        judgmentConfidence: (i.judgment_confidence as number) ?? null,
        createdAt: (i.created_at as string) || '',
        updatedAt: (i.updated_at as string) || '',
        engine: (i.engine as string) || parseEngine(sk),
      }
    })

  return { rcaId: id, session: sessionData, spans, hypotheses }
})
