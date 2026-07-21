import { GetObjectCommand } from '@aws-sdk/client-s3'
import { GetCommand } from '@aws-sdk/lib-dynamodb'

const ALLOWED_ENGINES = new Set(['strands', 'cc-headless'])

async function fetchReport(bucket: string, key: string, s3: ReturnType<typeof useS3>) {
  const resp = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: key }))
  return (await resp.Body?.transformToString()) ?? ''
}

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing report id' })
  }

  const query = getQuery(event)
  const engineFilter = (query.engine as string) || ''
  if (engineFilter && !ALLOWED_ENGINES.has(engineFilter)) {
    throw createError({ statusCode: 400, statusMessage: 'Invalid engine' })
  }

  const config = useRuntimeConfig()
  const ddb = useDynamoDB()
  const s3 = useS3()

  const engines = engineFilter
    ? [engineFilter]
    : ['cc-headless', 'strands']

  const attempts: string[] = []
  for (const engine of engines) {
    const sessionKeys = [`${engine}#SESSION`]
    if (engine === 'strands') sessionKeys.push('SESSION')

    let reportKey = ''
    for (const sessionKey of sessionKeys) {
      const sessionResult = await ddb.send(
        new GetCommand({
          TableName: config.dynamodbTableName,
          Key: { PK: `RCA#${id}`, SK: sessionKey },
          ProjectionExpression: 'report_s3_key',
        }),
      )
      reportKey = typeof sessionResult.Item?.report_s3_key === 'string'
        ? sessionResult.Item.report_s3_key
        : ''
      if (reportKey) break
    }

    const keys = [reportKey, `reports/${engine}/${id}.md`].filter(
      (key, index, all): key is string => Boolean(key) && all.indexOf(key) === index,
    )
    for (const key of keys) {
      attempts.push(key)
      try {
        const markdown = await fetchReport(config.s3ReportBucket, key, s3)
        return { rcaId: id, engine, markdown }
      } catch (err: any) {
        if (err.name !== 'NoSuchKey') {
          throw err
        }
      }
    }
  }

  // Legacy fallback (pre engine-split uploads).
  if (!engineFilter) {
    const legacyKey = `reports/${id}.md`
    attempts.push(legacyKey)
    try {
      const markdown = await fetchReport(config.s3ReportBucket, legacyKey, s3)
      return { rcaId: id, engine: 'legacy', markdown }
    } catch (err: any) {
      if (err.name !== 'NoSuchKey') {
        throw err
      }
    }
  }

  throw createError({
    statusCode: 404,
    statusMessage: `Report not found (tried: ${attempts.join(', ')})`,
  })
})
