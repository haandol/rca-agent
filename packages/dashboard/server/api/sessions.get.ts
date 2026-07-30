import { ScanCommand, type ScanCommandInput } from '@aws-sdk/lib-dynamodb';

export default defineEventHandler(async () => {
  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const items = [];
  let exclusiveStartKey: ScanCommandInput['ExclusiveStartKey'];

  do {
    const result = await ddb.send(
      new ScanCommand({
        TableName: config.dynamodbTableName,
        // Sessions and execution items share the partition, so the scan collects
        // both and each is recognised by its sort key below.
        FilterExpression:
          '(contains(SK, :session_suffix) OR begins_with(SK, :execution_prefix))' +
          ' AND begins_with(PK, :prefix)',
        ExpressionAttributeValues: {
          ':session_suffix': 'SESSION',
          ':execution_prefix': 'EXEC#',
          ':prefix': 'RCA#',
        },
        ExclusiveStartKey: exclusiveStartKey,
      }),
    );
    items.push(...(result.Items ?? []));
    exclusiveStartKey = result.LastEvaluatedKey;
  } while (exclusiveStartKey);

  // Executions have their own lifecycle, so they are attached to the session row
  // rather than folded into its state: an execution failure must not make a
  // finished analysis look failed.
  const executionsByRca = new Map<string, ExecutionSummary[]>();
  for (const item of items) {
    if (!isExecutionItem((item.SK as string) || '')) continue;
    const execution = readExecution(item);
    const existing = executionsByRca.get(execution.rcaId);
    if (existing) existing.push(execution);
    else executionsByRca.set(execution.rcaId, [execution]);
  }

  const sessions = items
    .filter((item) => !isExecutionItem((item.SK as string) || ''))
    .map((item) => {
      const rcaId = (item.PK as string).replace('RCA#', '');
      const engine =
        (item.engine as string) ||
        ((item.SK as string) === 'SESSION'
          ? 'strands'
          : (item.SK as string).split('#SESSION')[0]) ||
        'strands';
      const executions = (executionsByRca.get(rcaId) ?? []).filter(
        (execution) => !execution.engine || execution.engine === engine,
      );
      const execution = latestExecution(executions);

      return {
        rcaId,
        state: (item.state as string) || 'UNKNOWN',
        alarmName: (item.alarm_name as string) || 'N/A',
        alarmArn: (item.alarm_arn as string) || '',
        rootCause: (item.root_cause as string) || '',
        confirmed: (item.confirmed as boolean) ?? false,
        errorReason: (item.error_reason as string) || '',
        createdAt: (item.created_at as string) || '',
        updatedAt: (item.updated_at as string) || '',
        engine,
        executionState: execution?.state ?? '',
        executionStateLabel: execution?.stateLabel ?? '',
        executionId: execution?.executionId ?? '',
        executionAttempts: executions.length,
        executionBlockedCount: execution?.blockedCount ?? 0,
        retrospectiveStatus: execution?.retrospectiveStatus ?? '',
      };
    })
    .sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''));

  return sessions;
});
