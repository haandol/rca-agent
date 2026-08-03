import { QueryCommand, type QueryCommandInput } from '@aws-sdk/lib-dynamodb';

/**
 * One page of the session list, newest first.
 *
 * Read from the session-list index rather than by scanning: a scan reads every
 * span and hypothesis in the table and discards them, so its cost tracked trace
 * volume rather than session count, and it returns no ordering — "the newest 25"
 * had no meaning. The index is keyed by attributes only session records carry, so
 * one query per engine returns sessions alone, already sorted.
 *
 * Rows are enriched only for the sessions on this page. Deciding whether a report
 * still awaits approval needs its partition's playbook steps and execution items,
 * and doing that for the whole archive would undo the paging. The archive-wide
 * counts a reader needs are a separate endpoint for the same reason.
 */
const DEFAULT_PAGE_SIZE = 25;
const MAX_PAGE_SIZE = 100;

export default defineEventHandler(async (event) => {
  const config = useRuntimeConfig();
  const ddb = useDynamoDB();

  const query = getQuery(event);
  const requested = Number.parseInt(String(query.limit ?? ''), 10);
  const pageSize = Number.isFinite(requested)
    ? Math.min(Math.max(requested, 1), MAX_PAGE_SIZE)
    : DEFAULT_PAGE_SIZE;

  const positions = decodeCursor(query.cursor);
  const startedFrom = new Map(
    positions.map((position) => [position.engine, position.lastKey]),
  );

  // Only engines this dashboard acts on. The cursor is caller-supplied, so the
  // set of partitions read is decided here rather than by the request.
  const engines = query.cursor
    ? ALLOWED_ENGINES.filter((engine) => startedFrom.has(engine))
    : [...ALLOWED_ENGINES];

  /**
   * Each engine is its own index partition, so a page is the merge of one query
   * per engine. Each is asked for a full page: the newest `pageSize` overall can
   * come entirely from one engine, and asking for half from each would drop the
   * older of the two whenever they are unevenly spaced.
   */
  const perEngine = await Promise.all(
    engines.map(async (engine) => {
      const result = await ddb.send(
        new QueryCommand({
          TableName: config.dynamodbTableName,
          IndexName: SESSION_LIST_INDEX,
          KeyConditionExpression: '#pk = :engine',
          ExpressionAttributeNames: { '#pk': LIST_PARTITION_KEY },
          ExpressionAttributeValues: { ':engine': engine },
          // Newest first: the list is a record read from the present backwards.
          ScanIndexForward: false,
          Limit: pageSize,
          ExclusiveStartKey: startedFrom.get(engine) as
            QueryCommandInput['ExclusiveStartKey'] | undefined,
        }),
      );
      return {
        engine,
        items: result.Items ?? [],
        lastKey: result.LastEvaluatedKey,
      };
    }),
  );

  const merged = perEngine
    .flatMap(({ engine, items }) =>
      items.map((item) => ({
        item,
        engine: (item.engine as string) || engine,
        createdAt: (item.created_at as string) || '',
      })),
    )
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
    .slice(0, pageSize);

  /**
   * Where the next page resumes.
   *
   * A merged page usually leaves part of one engine's query unconsumed, so the
   * cursor cannot be the raw `LastEvaluatedKey` — that would skip the rows this
   * page trimmed. Each engine resumes from its own last row that actually shipped,
   * and an engine whose rows were all consumed with no more to give drops out.
   */
  const nextPositions: EnginePosition[] = [];
  for (const { engine, items, lastKey } of perEngine) {
    const shipped = merged.filter((row) => row.engine === engine).length;
    if (shipped < items.length) {
      const lastShipped = items[shipped - 1];
      if (lastShipped) {
        nextPositions.push({
          engine,
          lastKey: {
            PK: lastShipped.PK,
            SK: lastShipped.SK,
            [LIST_PARTITION_KEY]: lastShipped[LIST_PARTITION_KEY],
            [LIST_SORT_KEY]: lastShipped[LIST_SORT_KEY],
          },
        });
      }
    } else if (lastKey) {
      nextPositions.push({ engine, lastKey });
    }
  }

  // Enrichment is per-partition, so it runs only for the rows on this page.
  const sessions = await Promise.all(
    merged.map(async ({ item, engine }) => {
      const rcaId = rcaIdFromPk(item.PK as string);
      const partition = await ddb.send(
        new QueryCommand({
          TableName: config.dynamodbTableName,
          KeyConditionExpression: 'PK = :pk',
          ExpressionAttributeValues: { ':pk': rcaPk(rcaId) },
        }),
      );
      const partitionItems = partition.Items ?? [];

      // Executions have their own lifecycle, so they are attached to the row
      // rather than folded into its state: an execution failure must not make a
      // finished analysis look failed.
      const executions = partitionItems
        .filter((entry) => isExecutionItem((entry.SK as string) || ''))
        .map(readExecution)
        .filter(
          (execution) => !execution.engine || execution.engine === engine,
        );
      const execution = latestExecution(executions);

      // A terminal state overwrites the stage it happened in, so how far a
      // stopped run got is only recoverable from its spans.
      const spans = partitionItems
        .filter((entry) => isSpanSortKey((entry.SK as string) || ''))
        .map((entry) => ({
          spanType: (entry.span_type as string) || '',
          engine:
            (entry.engine as string) || parseEngine((entry.SK as string) || ''),
        }))
        .filter((span) => span.engine === engine);

      const state = (item.state as string) || 'UNKNOWN';
      const stepCount = countExecutionSteps(partitionItems, engine);
      const readiness = readinessOf({
        state,
        stepCount,
        hasExecution: executions.length > 0,
      });

      return {
        rcaId,
        state,
        readiness,
        readinessLabel: READINESS_LABEL[readiness],
        executionStepCount: stepCount,
        alarmName: (item.alarm_name as string) || 'N/A',
        alarmArn: (item.alarm_arn as string) || '',
        rootCause: (item.root_cause as string) || '',
        confirmed: (item.confirmed as boolean) ?? false,
        stoppedAt: furthestStage(spans, engine),
        // Strands records a skipped-for-age reason in error_reason, CC Headless
        // in outdated_reason. Read both so an OUTDATED session explains itself
        // regardless of which engine produced it.
        errorReason:
          (item.error_reason as string) ||
          (item.outdated_reason as string) ||
          '',
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
    }),
  );

  return {
    sessions,
    nextCursor: encodeCursor(nextPositions),
  };
});
