import { GetCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';
import { GetObjectCommand } from '@aws-sdk/client-s3';

/**
 * The four things a retrospective's update must be read against.
 *
 * A retrospective revises a playbook automatically, so its update is only
 * trustworthy if a person can see what it was based on. Returning these
 * separately would leave the reader assembling them by hand, and the
 * pre-execution playbook in particular cannot be recovered later — the revision
 * overwrote it.
 */
export default defineEventHandler(async (event) => {
  const rcaId = getRouterParam(event, 'rcaId');
  const executionId = getRouterParam(event, 'executionId');
  if (!rcaId || !executionId) {
    throw createError({
      statusCode: 400,
      statusMessage: 'Missing RCA id or execution id',
    });
  }

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const executionItem = await ddb.send(
    new GetCommand({
      TableName: config.dynamodbTableName,
      Key: { PK: `RCA#${rcaId}`, SK: `EXEC#${executionId}` },
    }),
  );
  if (!executionItem.Item) {
    throw createError({
      statusCode: 404,
      statusMessage: 'Execution not found',
    });
  }
  const execution = readExecution(executionItem.Item);

  const session = await ddb.send(
    new GetCommand({
      TableName: config.dynamodbTableName,
      Key: { PK: `RCA#${rcaId}`, SK: `${execution.engine}#SESSION` },
    }),
  );

  const [evidence, playbookBefore, diff] = await Promise.all([
    readJsonObject(config.s3ReportBucket, execution.evidenceS3Key),
    readJsonObject(config.s3ReportBucket, execution.playbookSnapshotS3Key),
    readJsonObject(config.s3ReportBucket, execution.retrospectiveDiffS3Key),
  ]);

  const revision = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk AND SK = :sk',
      ExpressionAttributeValues: {
        ':pk': `RCA#${rcaId}`,
        ':sk': `${execution.engine}#PLAYBOOK_REVISION`,
      },
    }),
  );
  const revisionItem = revision.Items?.[0];

  return {
    rcaId,
    executionId,
    // 1. The issue.
    issue: {
      alarmName: (session.Item?.alarm_name as string) || '',
      rootCause: (session.Item?.root_cause as string) || '',
      confirmed: (session.Item?.confirmed as boolean) ?? false,
      engine: execution.engine,
      reportS3Key: (session.Item?.report_s3_key as string) || '',
    },
    // 2. The playbook as it stood before the execution ran.
    playbookBefore,
    // 3. What was attempted, what failed, and what was corrected.
    evidence,
    // 4. How the procedure changed as a result.
    diff,
    playbookAfter: safeParse(revisionItem?.playbook),
    revisedByExecutionId:
      (revisionItem?.revised_by_execution_id as string) || '',
    execution,
  };
});

async function readJsonObject(
  bucket: string,
  key: string,
): Promise<Record<string, unknown> | null> {
  if (!bucket || !key) return null;
  try {
    const result = await useS3().send(
      new GetObjectCommand({ Bucket: bucket, Key: key }),
    );
    const body = await result.Body?.transformToString();
    return safeParse(body);
  } catch {
    // A cleaned-up object means the four-part comparison is incomplete, which the
    // page shows as a gap rather than failing the whole request.
    return null;
  }
}

function safeParse(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value);
    return parsed !== null &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}
