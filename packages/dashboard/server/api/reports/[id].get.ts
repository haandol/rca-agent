import { GetObjectCommand } from '@aws-sdk/client-s3'

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

  const config = useRuntimeConfig()
  const s3 = useS3()

  const engines = engineFilter && ALLOWED_ENGINES.has(engineFilter)
    ? [engineFilter]
    : ['cc-headless', 'strands']

  const attempts: string[] = []
  for (const engine of engines) {
    const key = `reports/${engine}/${id}.md`
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
