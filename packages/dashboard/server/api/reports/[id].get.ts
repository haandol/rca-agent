import { GetObjectCommand } from '@aws-sdk/client-s3'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')
  if (!id) {
    throw createError({ statusCode: 400, statusMessage: 'Missing report id' })
  }

  const config = useRuntimeConfig()
  const s3 = useS3()

  try {
    const resp = await s3.send(
      new GetObjectCommand({
        Bucket: config.s3ReportBucket,
        Key: `reports/${id}.md`,
      }),
    )
    const body = await resp.Body?.transformToString()
    return { rcaId: id, markdown: body ?? '' }
  } catch (err: any) {
    if (err.name === 'NoSuchKey') {
      throw createError({ statusCode: 404, statusMessage: 'Report not found' })
    }
    throw err
  }
})
