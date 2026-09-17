import { GetObjectCommand } from '@aws-sdk/client-s3';
import { GetCommand } from '@aws-sdk/lib-dynamodb';

/** Read the analysis-time object without rewriting it with current execution state. */
async function fetchReport(
  bucket: string,
  key: string,
  s3: ReturnType<typeof useS3>,
) {
  const resp = await s3.send(
    new GetObjectCommand({ Bucket: bucket, Key: key }),
  );
  return (await resp.Body?.transformToString()) ?? '';
}

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id');
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing report id' });
  }

  const query = getQuery(event);
  const engineFilter = (query.engine as string) || '';
  if (engineFilter && !isAllowedEngine(engineFilter)) {
    throw createError({ statusCode: 400, statusMessage: 'Invalid engine' });
  }

  const config = useRuntimeConfig();
  const ddb = useDynamoDB();
  const s3 = useS3();

  const engines = engineFilter
    ? [engineFilter]
    : ['headless-codex', 'codex-headless', 'cc-headless', 'strands'];

  const attempts: string[] = [];
  let completedSessionFound = false;
  for (const engine of engines) {
    let reportKey = '';
    let completed = false;
    let finalizedFailure = false;
    let summaryAuthority: Record<string, unknown> = {};
    for (const sessionKey of sessionSkCandidates(engine)) {
      const sessionResult = await ddb.send(
        new GetCommand({
          TableName: config.dynamodbTableName,
          Key: { PK: rcaPk(id), SK: sessionKey },
          ProjectionExpression:
            'report_s3_key, engine, #st, root_cause, confirmed, confidence_score, workflow, analysis_parts_finalized',
          ExpressionAttributeNames: { '#st': 'state' },
        }),
      );
      if (sessionResult.Item?.engine && sessionResult.Item.engine !== engine)
        continue;
      const candidate = sessionResult.Item;
      const storedKey =
        typeof candidate?.report_s3_key === 'string'
          ? candidate.report_s3_key
          : '';
      const persistedFailure =
        candidate?.state === 'FAILED' &&
        candidate.engine === engine &&
        candidate.workflow === 'recovery-first-v1' &&
        candidate.analysis_parts_finalized === true &&
        Boolean(storedKey.trim());
      if (candidate?.state !== 'COMPLETED' && !persistedFailure) continue;
      completed = true;
      finalizedFailure = persistedFailure;
      if (candidate?.state === 'COMPLETED') completedSessionFound = true;
      summaryAuthority = candidate ?? {};
      reportKey = storedKey;
      if (reportKey) break;
    }
    if (!completed) continue;

    // Failed finalized reports grant read access to the persisted key only, never an inferred fallback.
    const keys = (
      finalizedFailure ? [reportKey] : [reportKey, `reports/${engine}/${id}.md`]
    ).filter(
      (key, index, all): key is string =>
        Boolean(key) && all.indexOf(key) === index,
    );
    for (const key of keys) {
      attempts.push(key);
      try {
        const markdown = await fetchReport(config.s3ReportBucket, key, s3);
        return {
          rcaId: id,
          engine,
          markdown,
          displayMarkdown: reportDisplayMarkdown(markdown),
          summary: readReportSummary(markdown, summaryAuthority),
        };
      } catch (err: any) {
        if (err.name !== 'NoSuchKey') {
          throw err;
        }
      }
    }
  }

  // Legacy fallback (pre engine-split uploads).
  if (!engineFilter && completedSessionFound) {
    const legacyKey = `reports/${id}.md`;
    attempts.push(legacyKey);
    try {
      const markdown = await fetchReport(config.s3ReportBucket, legacyKey, s3);
      return {
        rcaId: id,
        engine: 'legacy',
        markdown,
        displayMarkdown: reportDisplayMarkdown(markdown),
        summary: readReportSummary(markdown),
      };
    } catch (err: any) {
      if (err.name !== 'NoSuchKey') {
        throw err;
      }
    }
  }

  throw createError({
    statusCode: 404,
    statusMessage: `Report not found (tried: ${attempts.join(', ')})`,
  });
});
