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
    throw createError({
      statusCode: 503,
      statusMessage:
        'EXECUTION_QUEUE_URL is not configured, so no approval can be published',
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
    throw createError({ statusCode: 404, statusMessage: 'Session not found' });
  }
  if (session.Item.state !== 'COMPLETED') {
    // An unfinished analysis has no approved report behind it.
    throw createError({
      statusCode: 409,
      statusMessage: `Analysis is ${session.Item.state ?? 'unknown'}, not COMPLETED`,
    });
  }

  const steps = await approvedExecutionSteps(ddb, config, rcaId, engine);
  if (!steps.length) {
    // An unconfirmed root cause carries no execution steps, so there is nothing
    // a person could have approved here.
    throw createError({
      statusCode: 409,
      statusMessage: 'The report declares no playbook execution steps to run',
    });
  }

  const running = await inFlightExecution(ddb, config, rcaId);
  if (running) {
    throw createError({
      statusCode: 409,
      statusMessage: `An execution is already ${running.stateLabel} for this report`,
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
