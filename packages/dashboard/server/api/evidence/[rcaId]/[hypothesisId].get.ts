import { GetObjectCommand } from '@aws-sdk/client-s3'

export default defineEventHandler(async (event) => {
  const rcaId = getRouterParam(event, 'rcaId')
  const hypothesisId = getRouterParam(event, 'hypothesisId')
  if (!rcaId || !hypothesisId) {
    throw createError({ statusCode: 400, statusMessage: 'Missing rcaId or hypothesisId' })
  }

  const config = useRuntimeConfig()
  const s3 = useS3()

  try {
    const resp = await s3.send(
      new GetObjectCommand({
        Bucket: config.s3ReportBucket,
        Key: `rca/${rcaId}/evidence/${hypothesisId}/combined.md`,
      }),
    )
    const body = await resp.Body?.transformToString()
    return { rcaId, hypothesisId, markdown: body ?? '' }
  } catch (err: any) {
    if (err.name === 'NoSuchKey') {
      throw createError({ statusCode: 404, statusMessage: 'Evidence not found' })
    }
    throw err
  }
})
