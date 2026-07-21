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
        FilterExpression:
          'contains(SK, :session_suffix) AND begins_with(PK, :prefix)',
        ExpressionAttributeValues: {
          ':session_suffix': 'SESSION',
          ':prefix': 'RCA#',
        },
        ExclusiveStartKey: exclusiveStartKey,
      }),
    );
    items.push(...(result.Items ?? []));
    exclusiveStartKey = result.LastEvaluatedKey;
  } while (exclusiveStartKey);

  const sessions = items
    .map((item) => {
      const remediation = readSessionRemediation(item);

      return {
        rcaId: (item.PK as string).replace('RCA#', ''),
        state: (item.state as string) || 'UNKNOWN',
        alarmName: (item.alarm_name as string) || 'N/A',
        alarmArn: (item.alarm_arn as string) || '',
        rootCause: (item.root_cause as string) || '',
        confirmed: (item.confirmed as boolean) ?? false,
        errorReason: (item.error_reason as string) || '',
        createdAt: (item.created_at as string) || '',
        updatedAt: (item.updated_at as string) || '',
        engine:
          (item.engine as string) ||
          ((item.SK as string) === 'SESSION'
            ? 'strands'
            : (item.SK as string).split('#SESSION')[0]) ||
          'strands',
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
    .sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''));

  return sessions;
});
