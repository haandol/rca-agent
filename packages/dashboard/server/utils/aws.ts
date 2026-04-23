import { DynamoDBClient } from '@aws-sdk/client-dynamodb'
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb'
import { S3Client } from '@aws-sdk/client-s3'

let _ddbDoc: DynamoDBDocumentClient | null = null
let _s3: S3Client | null = null

export function useDynamoDB(): DynamoDBDocumentClient {
  if (!_ddbDoc) {
    const config = useRuntimeConfig()
    const client = new DynamoDBClient({ region: config.awsRegion })
    _ddbDoc = DynamoDBDocumentClient.from(client)
  }
  return _ddbDoc
}

export function useS3(): S3Client {
  if (!_s3) {
    const config = useRuntimeConfig()
    _s3 = new S3Client({ region: config.awsRegion })
  }
  return _s3
}
