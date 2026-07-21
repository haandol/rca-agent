import { QueryCommand, type QueryCommandInput } from '@aws-sdk/lib-dynamodb';

function parseEngine(sk: string): string {
  if (sk === 'SESSION' || sk.startsWith('SPAN#') || sk.startsWith('HYPO#'))
    return 'strands';
  return sk.split('#')[0] ?? 'strands';
}

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
        ExpressionAttributeValues: { ':pk': `RCA#${id}` },
        ExclusiveStartKey: exclusiveStartKey,
      }),
    );
    items.push(...(result.Items ?? []));
    exclusiveStartKey = result.LastEvaluatedKey;
  } while (exclusiveStartKey);

  function matchesEngine(sk: string): boolean {
    if (!engineFilter) return true;
    const itemEngine = parseEngine(sk);
    return itemEngine === engineFilter;
  }

  const spans = items
    .filter((i) => {
      const sk = (i.SK as string) || '';
      return (
        (sk.includes('#SPAN#') || sk.startsWith('SPAN#')) && matchesEngine(sk)
      );
    })
    .map((i) => {
      const sk = i.SK as string;
      const spanId = sk.includes('#SPAN#')
        ? (sk.split('#SPAN#')[1] ?? '')
        : sk.replace('SPAN#', '');
      const remediation = readSpanRemediation(i);
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
        remediationStatus: remediation.remediationStatus,
        remediationSuccess: remediation.remediationSuccess,
        remediationSummary: remediation.remediationSummary,
        remediationError: remediation.remediationError,
        remediationCompletedAt: remediation.remediationCompletedAt,
        verificationStatus: remediation.verificationStatus,
        metricsNormalized: remediation.metricsNormalized,
        verificationSummary: remediation.verificationSummary,
        remainingIssues: remediation.remainingIssues,
        remediationFaultType: remediation.remediationFaultType,
        remediationEndpoint: remediation.remediationEndpoint,
      };
    })
    .sort((a, b) => a.startTime.localeCompare(b.startTime));

  const session = items.find((i) => {
    const sk = i.SK as string;
    const isSession = sk.endsWith('#SESSION') || sk === 'SESSION';
    return isSession && matchesEngine(sk);
  });
  const selectedSessionEngine = session
    ? (session.engine as string) || parseEngine(session.SK as string)
    : engineFilter;
  const remediationSpan = [...spans]
    .reverse()
    .find(
      (span) =>
        span.spanType === 'REMEDIATION' &&
        span.engine === selectedSessionEngine,
    );
  const sessionRemediation = session
    ? mergeRemediationDetails(
        readSessionRemediation(session),
        remediationSpan
          ? {
              remediationStatus: remediationSpan.remediationStatus,
              remediationSuccess: remediationSpan.remediationSuccess,
              remediationSummary: remediationSpan.remediationSummary,
              remediationError: remediationSpan.remediationError,
              remediationCompletedAt: remediationSpan.remediationCompletedAt,
              verificationStatus: remediationSpan.verificationStatus,
              metricsNormalized: remediationSpan.metricsNormalized,
              verificationSummary: remediationSpan.verificationSummary,
              remainingIssues: remediationSpan.remainingIssues,
              remediationFaultType: remediationSpan.remediationFaultType,
              remediationEndpoint: remediationSpan.remediationEndpoint,
            }
          : readSpanRemediation({}),
      )
    : null;
  const sessionData =
    session && sessionRemediation
      ? {
          state: (session.state as string) || 'UNKNOWN',
          alarmName: (session.alarm_name as string) || '',
          alarmArn: (session.alarm_arn as string) || '',
          rootCause: (session.root_cause as string) || '',
          confirmed: (session.confirmed as boolean) ?? false,
          errorReason: (session.error_reason as string) || '',
          createdAt: (session.created_at as string) || '',
          updatedAt: (session.updated_at as string) || '',
          engine:
            (session.engine as string) || parseEngine(session.SK as string),
          remediationStatus: sessionRemediation.remediationStatus,
          remediationSuccess: sessionRemediation.remediationSuccess,
          remediationSummary: sessionRemediation.remediationSummary,
          remediationError: sessionRemediation.remediationError,
          remediationCompletedAt: sessionRemediation.remediationCompletedAt,
          verificationStatus: sessionRemediation.verificationStatus,
          metricsNormalized: sessionRemediation.metricsNormalized,
          verificationSummary: sessionRemediation.verificationSummary,
          remainingIssues: sessionRemediation.remainingIssues,
          remediationFaultType: sessionRemediation.remediationFaultType,
          remediationEndpoint: sessionRemediation.remediationEndpoint,
        }
      : null;

  const hypotheses = items
    .filter((i) => {
      const sk = (i.SK as string) || '';
      return (
        (sk.includes('#HYPO#') || sk.startsWith('HYPO#')) && matchesEngine(sk)
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

  return { rcaId: id, session: sessionData, spans, hypotheses };
});
