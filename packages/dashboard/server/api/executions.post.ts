import {
  GetObjectCommand,
  HeadObjectCommand,
  PutObjectCommand,
} from '@aws-sdk/client-s3';
import { SendMessageCommand } from '@aws-sdk/client-sqs';
import {
  GetCommand,
  QueryCommand,
  TransactWriteCommand,
  type QueryCommandInput,
} from '@aws-sdk/lib-dynamodb';

type DataRecord = Record<string, unknown>;

/**
 * Persist the approval before publishing it. The worker will only claim a queue
 * message whose exact fields already exist in this PENDING_APPROVAL item.
 */
export default defineEventHandler(async (event) => {
  const body = await readBody<{
    rcaId?: string;
    engine?: string;
    approvalId?: string;
  }>(event);

  const rcaId = typeof body?.rcaId === 'string' ? body.rcaId.trim() : '';
  const engine = typeof body?.engine === 'string' ? body.engine.trim() : '';
  const approvalId =
    typeof body?.approvalId === 'string' ? body.approvalId.trim() : '';

  if (!rcaId) {
    throw createError({ statusCode: 400, statusMessage: 'Missing rcaId' });
  }
  if (!isAllowedEngine(engine)) {
    throw createError({
      statusCode: 400,
      statusMessage: 'Missing or invalid engine',
    });
  }
  if (!isUuid(approvalId)) {
    throw createError({
      statusCode: 400,
      statusMessage: 'approvalId must be a UUID',
    });
  }

  const config = useRuntimeConfig();
  if (!config.executionQueueUrl) {
    throw createError({
      statusCode: 503,
      statusMessage:
        '실행 요청 큐가 설정되지 않아 승인을 발행할 수 없습니다. EXECUTION_QUEUE_URL 환경변수를 확인하세요.',
    });
  }

  const ddb = useDynamoDB();
  const items = await readPartition(
    ddb,
    config.dynamodbTableName,
    rcaPk(rcaId),
  );
  const session = findSessionForEngine(items, engine);
  if (!session) {
    throw createError({
      statusCode: 404,
      statusMessage: '세션을 찾을 수 없습니다.',
    });
  }
  if (session.state !== 'COMPLETED') {
    throw createError({
      statusCode: 409,
      statusMessage: '분석이 완료되지 않아 승인할 수 없습니다.',
    });
  }
  if (session.confirmed !== true) {
    throw createError({
      statusCode: 409,
      statusMessage: '근본원인이 확정되지 않아 승인할 수 없습니다.',
    });
  }

  const reportS3Key =
    typeof session.report_s3_key === 'string'
      ? session.report_s3_key.trim()
      : '';
  if (!reportS3Key) {
    throw createError({
      statusCode: 409,
      statusMessage: '승인할 리포트의 저장 위치가 없습니다.',
    });
  }
  await requireReportObject(useS3(), config.s3ReportBucket, reportS3Key);

  const resolved = resolveCurrentPlaybook(items, session, engine);
  const validation = validateExecutablePlaybook(resolved?.playbook ?? null);
  if (!resolved || !validation.valid) {
    throw createError({
      statusCode: 409,
      statusMessage:
        validation.reason ||
        '이 세션의 현재 플레이북을 정확히 확인할 수 없습니다.',
    });
  }

  const snapshotBytes = serializePlaybookSnapshot(resolved.playbook);
  const playbookDigest = sha256Hex(snapshotBytes);
  const executionId = approvalId;
  const approvedPlaybookS3Key = `approvals/${rcaId}/${executionId}/playbook.json`;

  await storeImmutableSnapshot({
    bucket: config.s3ReportBucket,
    key: approvedPlaybookS3Key,
    bytes: snapshotBytes,
    digest: playbookDigest,
  });

  const request: ExecutionRequestFields = {
    execution_id: executionId,
    rca_id: rcaId,
    engine,
    approval_id: approvalId,
    requested_by: APPROVAL_REQUESTED_BY,
    report_s3_key: reportS3Key,
    approved_playbook_s3_key: approvedPlaybookS3Key,
    playbook_digest: playbookDigest,
  };
  const reserved = await reserveExecution({
    ddb,
    tableName: config.dynamodbTableName,
    request,
  });

  try {
    await useSqs().send(
      new SendMessageCommand({
        QueueUrl: config.executionQueueUrl,
        MessageBody: JSON.stringify(request),
      }),
    );
  } catch {
    throw createError({
      statusCode: 503,
      statusMessage:
        '실행 요청 전송에 실패했습니다. 승인은 보존되었으므로 같은 요청으로 다시 시도하세요.',
    });
  }

  return {
    requested: true,
    reserved,
    rcaId,
    engine,
    approvalId,
    executionId,
    stepCount: validation.steps.length,
    approvedPlaybookS3Key,
    playbookDigest,
  };
});

async function readPartition(
  ddb: ReturnType<typeof useDynamoDB>,
  tableName: string,
  partitionKey: string,
): Promise<DataRecord[]> {
  const items: DataRecord[] = [];
  let startKey: QueryCommandInput['ExclusiveStartKey'];
  do {
    const result = await ddb.send(
      new QueryCommand({
        TableName: tableName,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': partitionKey },
        ExclusiveStartKey: startKey,
      }),
    );
    items.push(...(result.Items ?? []));
    startKey = result.LastEvaluatedKey;
  } while (startKey);
  return items;
}

async function requireReportObject(
  s3: ReturnType<typeof useS3>,
  bucket: string,
  key: string,
): Promise<void> {
  try {
    await s3.send(new HeadObjectCommand({ Bucket: bucket, Key: key }));
  } catch (error) {
    if (isMissingObject(error)) {
      throw createError({
        statusCode: 409,
        statusMessage: '승인할 리포트 객체가 S3에 없습니다.',
      });
    }
    throw createError({
      statusCode: 503,
      statusMessage: '리포트 객체를 확인할 수 없습니다.',
    });
  }
}

async function storeImmutableSnapshot({
  bucket,
  key,
  bytes,
  digest,
}: {
  bucket: string;
  key: string;
  bytes: Uint8Array;
  digest: string;
}): Promise<void> {
  const s3 = useS3();
  try {
    await s3.send(new HeadObjectCommand({ Bucket: bucket, Key: key }));
    await verifyExistingSnapshot(s3, bucket, key, digest);
    return;
  } catch (error) {
    if (!isMissingObject(error)) {
      if (isHttpError(error)) throw error;
      throw createError({
        statusCode: 503,
        statusMessage: '기존 승인 스냅샷을 확인할 수 없습니다.',
      });
    }
  }

  try {
    await s3.send(
      new PutObjectCommand({
        Bucket: bucket,
        Key: key,
        Body: bytes,
        ContentType: 'application/json; charset=utf-8',
        Metadata: { sha256: digest },
        IfNoneMatch: '*',
      }),
    );
  } catch (error) {
    if (httpStatus(error) === 412) {
      await verifyExistingSnapshot(s3, bucket, key, digest);
      return;
    }
    throw createError({
      statusCode: 503,
      statusMessage: '승인 플레이북 스냅샷을 저장할 수 없습니다.',
    });
  }
}

async function verifyExistingSnapshot(
  s3: ReturnType<typeof useS3>,
  bucket: string,
  key: string,
  expectedDigest: string,
): Promise<void> {
  try {
    const existing = await s3.send(
      new GetObjectCommand({ Bucket: bucket, Key: key }),
    );
    const bytes =
      (await existing.Body?.transformToByteArray()) ?? new Uint8Array();
    if (sha256Hex(bytes) !== expectedDigest) {
      throw createError({
        statusCode: 409,
        statusMessage:
          '같은 승인 식별자에 다른 플레이북 스냅샷이 이미 존재합니다.',
      });
    }
  } catch (error) {
    if (isHttpError(error)) throw error;
    throw createError({
      statusCode: 503,
      statusMessage: '기존 승인 스냅샷을 검증할 수 없습니다.',
    });
  }
}

async function reserveExecution({
  ddb,
  tableName,
  request,
}: {
  ddb: ReturnType<typeof useDynamoDB>;
  tableName: string;
  request: ExecutionRequestFields;
}): Promise<boolean> {
  const now = new Date().toISOString();
  const ttl = Math.floor(Date.now() / 1000) + APPROVAL_TTL_DAYS * 24 * 60 * 60;
  const executionItem = {
    PK: rcaPk(request.rca_id),
    SK: executionSk(request.execution_id),
    ...request,
    execution_state: 'PENDING_APPROVAL',
    attempt: 0,
    created_at: now,
    updated_at: now,
    ttl,
  };
  const activeItem = {
    PK: rcaPk(request.rca_id),
    SK: ACTIVE_EXECUTION_SK,
    execution_id: request.execution_id,
    engine: request.engine,
    created_at: now,
    updated_at: now,
    ttl,
  };

  try {
    await ddb.send(
      new TransactWriteCommand({
        TransactItems: [
          {
            Put: {
              TableName: tableName,
              Item: executionItem,
              ConditionExpression:
                'attribute_not_exists(PK) AND attribute_not_exists(SK)',
            },
          },
          {
            Put: {
              TableName: tableName,
              Item: activeItem,
              ConditionExpression:
                'attribute_not_exists(PK) AND attribute_not_exists(SK)',
            },
          },
        ],
      }),
    );
    return true;
  } catch (error) {
    const [execution, active] = await Promise.all([
      ddb.send(
        new GetCommand({
          TableName: tableName,
          Key: {
            PK: rcaPk(request.rca_id),
            SK: executionSk(request.execution_id),
          },
          ConsistentRead: true,
        }),
      ),
      ddb.send(
        new GetCommand({
          TableName: tableName,
          Key: { PK: rcaPk(request.rca_id), SK: ACTIVE_EXECUTION_SK },
          ConsistentRead: true,
        }),
      ),
    ]);

    if (
      executionReservationMatches(execution.Item, request) &&
      active.Item?.execution_id === request.execution_id
    ) {
      return false;
    }

    if (
      errorName(error) === 'TransactionCanceledException' ||
      execution.Item ||
      active.Item
    ) {
      throw createError({
        statusCode: 409,
        statusMessage:
          '다른 실행이 이미 활성 상태이거나 같은 승인 식별자의 내용이 일치하지 않습니다.',
      });
    }
    throw createError({
      statusCode: 503,
      statusMessage: '실행 승인을 예약할 수 없습니다. 다시 시도하세요.',
    });
  }
}

function errorName(error: unknown): string {
  return typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    typeof error.name === 'string'
    ? error.name
    : '';
}

function httpStatus(error: unknown): number {
  if (typeof error !== 'object' || error === null || !('$metadata' in error)) {
    return 0;
  }
  const metadata = error.$metadata as { httpStatusCode?: unknown };
  return typeof metadata.httpStatusCode === 'number'
    ? metadata.httpStatusCode
    : 0;
}

function isMissingObject(error: unknown): boolean {
  return (
    httpStatus(error) === 404 ||
    ['NotFound', 'NoSuchKey'].includes(errorName(error))
  );
}

function isHttpError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'statusCode' in error &&
    typeof error.statusCode === 'number'
  );
}
