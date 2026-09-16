import { ECSClient } from '@aws-sdk/client-ecs';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { S3Client } from '@aws-sdk/client-s3';
import { SQSClient } from '@aws-sdk/client-sqs';
import { S3VectorsClient } from '@aws-sdk/client-s3vectors';
import { BedrockRuntimeClient } from '@aws-sdk/client-bedrock-runtime';

let _ddbDoc: DynamoDBDocumentClient | null = null;
let _s3: S3Client | null = null;
let _sqs: SQSClient | null = null;
let _vectors: S3VectorsClient | null = null;
let _embedding: BedrockRuntimeClient | null = null;

/** Reuse the configured vector-region client for library reads and explicit publication. */
export function useS3Vectors(): S3VectorsClient {
  return (_vectors ??= new S3VectorsClient({
    region: useRuntimeConfig().s3VectorRegion,
  }));
}

/** Reuse the publication embedding client in the AWS region shared with the Python writers. */
export function useEmbeddingRuntime(): BedrockRuntimeClient {
  return (_embedding ??= new BedrockRuntimeClient({
    region: useRuntimeConfig().awsRegion,
  }));
}

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
 * This client publishes execution approvals. Knowledge proposal decisions write
 * separately to DynamoDB and the vector index. Both use the operator's local AWS
 * credentials; the dashboard is a local tool, not a deployed service.
 */
export function useSqs(): SQSClient {
  if (!_sqs) {
    const config = useRuntimeConfig();
    _sqs = new SQSClient({ region: config.awsRegion });
  }
  return _sqs;
}

const ecsByRegion = new Map<string, ECSClient>();
/** Server-only read client scoped to the validated stored deployment region. */
export function useEcs(region: string): ECSClient {
  let client = ecsByRegion.get(region);
  if (!client) {
    client = new ECSClient({ region });
    ecsByRegion.set(region, client);
  }
  return client;
}
