import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { S3Client } from '@aws-sdk/client-s3';
import { SQSClient } from '@aws-sdk/client-sqs';

let _ddbDoc: DynamoDBDocumentClient | null = null;
let _s3: S3Client | null = null;
let _sqs: SQSClient | null = null;

export function useDynamoDB(): DynamoDBDocumentClient {
  if (!_ddbDoc) {
    const config = useRuntimeConfig();
    const client = new DynamoDBClient({ region: config.awsRegion });
    _ddbDoc = DynamoDBDocumentClient.from(client);
  }
  return _ddbDoc;
}

export function useS3(): S3Client {
  if (!_s3) {
    const config = useRuntimeConfig();
    _s3 = new S3Client({ region: config.awsRegion });
  }
  return _s3;
}

/**
 * Publishes playbook execution requests.
 *
 * This is the dashboard's only write capability against a running system, and it
 * is bound to the local AWS credentials the operator already has — the dashboard
 * is a local tool, not a deployed service.
 */
export function useSqs(): SQSClient {
  if (!_sqs) {
    const config = useRuntimeConfig();
    _sqs = new SQSClient({ region: config.awsRegion });
  }
  return _sqs;
}
