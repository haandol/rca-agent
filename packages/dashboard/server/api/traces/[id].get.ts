import { QueryCommand } from '@aws-sdk/lib-dynamodb'

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

  const session = items.find((i) => i.SK === 'SESSION')
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
        engine: (session.engine as string) || 'strands',
      }
    : null

  const spans = items
    .filter((i) => ((i.SK as string) || '').startsWith('SPAN#'))
    .map((i) => ({
      spanId: (i.SK as string).replace('SPAN#', ''),
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
    }))
    .sort((a, b) => a.startTime.localeCompare(b.startTime))

  const hypotheses = items
    .filter((i) => ((i.SK as string) || '').startsWith('HYPO#'))
    .map((i) => ({
      hypothesisId: (i.SK as string).replace('HYPO#', ''),
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
    }))

  return { rcaId: id, session: sessionData, spans, hypotheses }
})
