import { GetCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';
import { SendMessageCommand } from '@aws-sdk/client-sqs';

/**
 * Publishes a playbook execution request — the only entry point to execution.
 *
 * Nothing else in the system can start an execution: the worker consumes this
 * queue and has no event subscription. So this handler is where a person's
 * approval becomes a message, and it refuses to publish anything the worker
 * would then reject.
 */
export default defineEventHandler(async (event) => {
  const body = await readBody<{
    rcaId?: string;
    engine?: string;
    approvalId?: string;
    requestedBy?: string;
  }>(event);

  const rcaId = typeof body?.rcaId === 'string' ? body.rcaId.trim() : '';
  const engine = typeof body?.engine === 'string' ? body.engine : '';
  if (!rcaId) {
    throw createError({ statusCode: 400, statusMessage: 'Missing rcaId' });
  }
  if (!isAllowedEngine(engine)) {
    throw createError({
      statusCode: 400,
      statusMessage: 'Missing or invalid engine',
    });
  }

  const config = useRuntimeConfig();
  if (!config.executionQueueUrl) {
    // Naming the variable is the point: this is a misconfiguration the person
    // reading it can fix, and failing loudly is what keeps a dashboard that
    // cannot publish from looking like it approved something.
    throw createError({
      statusCode: 503,
      statusMessage:
        '실행 요청 큐가 설정되지 않아 승인을 발행할 수 없습니다. EXECUTION_QUEUE_URL 환경변수를 확인하세요.',
    });
  }

  const ddb = useDynamoDB();

  const session = await ddb.send(
    new GetCommand({
      TableName: config.dynamodbTableName,
      Key: { PK: rcaPk(rcaId), SK: sessionSk(engine) },
    }),
  );
  if (!session.Item) {
    throw createError({
      statusCode: 404,
      statusMessage: '세션을 찾을 수 없습니다.',
    });
  }
  if (session.Item.state !== 'COMPLETED') {
    // An unfinished analysis has no approved report behind it.
    throw createError({
      statusCode: 409,
      statusMessage: '분석이 완료되지 않아 승인할 수 없습니다.',
    });
  }

  const steps = await approvedExecutionSteps(ddb, config, rcaId, engine);
  if (!steps.length) {
    // An unconfirmed root cause carries no execution steps, so there is nothing
    // a person could have approved here.
    throw createError({
      statusCode: 409,
      statusMessage:
        '이 리포트에는 실행할 절차가 없습니다. 근본원인이 확정되지 않았습니다.',
    });
  }

  const running = await inFlightExecution(ddb, config, rcaId);
  if (running) {
    throw createError({
      statusCode: 409,
      statusMessage: `아직 끝나지 않은 실행이 있습니다(${running.stateLabel}). 그 실행이 끝난 뒤에 다시 승인할 수 있습니다.`,
    });
  }

  // A stable approval identifier is what makes the request idempotent: the
  // worker derives the execution id from it, so a resubmitted approval claims
  // the same execution instead of running a second one.
  const approvalId =
    typeof body?.approvalId === 'string' && body.approvalId.trim()
      ? body.approvalId.trim()
      : `${rcaId}#${engine}#${new Date().toISOString()}`;

  await useSqs().send(
    new SendMessageCommand({
      QueueUrl: config.executionQueueUrl,
      MessageBody: JSON.stringify({
        rca_id: rcaId,
        engine,
        approval_id: approvalId,
        requested_by:
          typeof body?.requestedBy === 'string'
            ? body.requestedBy
            : 'dashboard',
        report_s3_key:
          typeof session.Item.report_s3_key === 'string'
            ? session.Item.report_s3_key
            : '',
      }),
    }),
  );

  return {
    requested: true,
    rcaId,
    engine,
    approvalId,
    stepCount: steps.length,
  };
});

async function approvedExecutionSteps(
  ddb: ReturnType<typeof useDynamoDB>,
  config: ReturnType<typeof useRuntimeConfig>,
  rcaId: string,
  engine: string,
): Promise<unknown[]> {
  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk',
      ExpressionAttributeValues: { ':pk': rcaPk(rcaId) },
    }),
  );
  const items = result.Items ?? [];

  // A retrospective revision, if one exists, is what the next execution runs.
  const revision = items.find(
    (item) => (item.SK as string) === playbookRevisionSk(engine),
  );
  if (revision) {
    const parsed = safeParse(revision.playbook);
    const steps = parsed?.execution_steps;
    if (Array.isArray(steps)) return steps;
  }

  const span = items.find(
    (item) =>
      (item.SK as string).startsWith(`${engine}#SPAN#`) &&
      item.span_type === 'PLAYBOOK',
  );
  const metadata = span?.metadata as Record<string, unknown> | undefined;
  const steps = metadata?.execution_steps;
  return Array.isArray(steps) ? steps : [];
}

async function inFlightExecution(
  ddb: ReturnType<typeof useDynamoDB>,
  config: ReturnType<typeof useRuntimeConfig>,
  rcaId: string,
): Promise<ExecutionSummary | null> {
  const result = await ddb.send(
    new QueryCommand({
      TableName: config.dynamodbTableName,
      KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
      ExpressionAttributeValues: {
        ':pk': rcaPk(rcaId),
        ':prefix': EXECUTION_SK_PREFIX,
      },
    }),
  );
  const executions = (result.Items ?? []).map(readExecution);
  return (
    executions.find((execution) => !isTerminalExecution(execution.state)) ??
    null
  );
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
