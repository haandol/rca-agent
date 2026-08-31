import { QueryCommand, type QueryCommandInput } from '@aws-sdk/lib-dynamodb';

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id');
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing RCA id' });
  }

  const query = getQuery(event);
  const engineFilter = (query.engine as string) || '';

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const items = [];
  let exclusiveStartKey: QueryCommandInput['ExclusiveStartKey'];

  do {
    const result = await ddb.send(
      new QueryCommand({
        TableName: config.dynamodbTableName,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': rcaPk(id) },
        ExclusiveStartKey: exclusiveStartKey,
      }),
    );
    items.push(...(result.Items ?? []));
    exclusiveStartKey = result.LastEvaluatedKey;
  } while (exclusiveStartKey);

  function matchesEngine(item: Record<string, unknown>): boolean {
    if (!engineFilter) return true;
    const sk = (item.SK as string) || '';
    const itemEngine = (item.engine as string) || parseEngine(sk);
    return itemEngine === engineFilter;
  }

  const spans = items
    .filter((i) => {
      const sk = (i.SK as string) || '';
      return (
        (sk.includes('#SPAN#') || sk.startsWith('SPAN#')) && matchesEngine(i)
      );
    })
    .map((i) => {
      const sk = i.SK as string;
      const spanId = sk.includes('#SPAN#')
        ? (sk.split('#SPAN#')[1] ?? '')
        : sk.replace('SPAN#', '');
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
      };
    })
    .sort((a, b) => a.startTime.localeCompare(b.startTime));

  const session = items.find((i) => {
    const sk = i.SK as string;
    const isSession = isSessionSortKey(sk);
    return isSession && matchesEngine(i);
  });
  // Executions have their own lifecycle, so they are reported alongside the
  // analysis session rather than merged into it.
  const executions = items
    .filter((i) => isExecutionItem((i.SK as string) || ''))
    .map(readExecution)
    .sort((a, b) => b.attempt - a.attempt);

  const sessionEngine = session
    ? (session.engine as string) || parseEngine(session.SK as string)
    : '';

  const sessionData = session
    ? {
        state: (session.state as string) || 'UNKNOWN',
        alarmName: (session.alarm_name as string) || '',
        alarmArn: (session.alarm_arn as string) || '',
        rootCause: (session.root_cause as string) || '',
        confirmed: (session.confirmed as boolean) ?? false,
        // A skipped-for-age reason lands in error_reason on Strands and in
        // outdated_reason on CC Headless, so both are read.
        errorReason:
          (session.error_reason as string) ||
          (session.outdated_reason as string) ||
          '',
        // The furthest stage the spans reached. A terminal state overwrites the
        // stage it happened in, so this is the only record of where a stopped run
        // actually got to.
        stoppedAt: furthestStage(spans, sessionEngine),
        createdAt: (session.created_at as string) || '',
        updatedAt: (session.updated_at as string) || '',
        engine: sessionEngine,
      }
    : null;

  const hypotheses = items
    .filter((i) => {
      const sk = (i.SK as string) || '';
      return (
        (sk.includes('#HYPO#') || sk.startsWith('HYPO#')) && matchesEngine(i)
      );
    })
    .map((i) => {
      const sk = i.SK as string;
      const hypothesisId = sk.includes('#HYPO#')
        ? (sk.split('#HYPO#')[1] ?? '')
        : sk.replace('HYPO#', '');
      return {
        hypothesisId,
        treeId: (i.tree_id as string) || '',
        parentId: (i.parent_id as string) || null,
        depth: (i.depth as number) ?? 0,
        title: (i.title as string) || '',
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
      };
    });

  return { rcaId: id, session: sessionData, spans, hypotheses, executions };
});
