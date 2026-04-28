import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3vectors from 'aws-cdk-lib/aws-s3vectors';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly evidenceBucketName: string;
  readonly vectorBucketName: string;
}

export class StorageStack extends cdk.Stack {
  readonly evidenceBucket: s3.IBucket;
  readonly vectorBucket: s3vectors.CfnVectorBucket;
  readonly evidenceIndex: s3vectors.CfnIndex;
  readonly playbookIndex: s3vectors.CfnIndex;
  readonly reportIndex: s3vectors.CfnIndex;

  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    this.evidenceBucket = this.newEvidenceBucket(props.evidenceBucketName);
    this.vectorBucket = this.newVectorBucket(props.vectorBucketName);
    this.evidenceIndex = this.newVectorIndex(
      'EvidenceIndex',
      'evidence',
      this.vectorBucket,
    );
    this.playbookIndex = this.newVectorIndex(
      'PlaybookIndex',
      'playbook',
      this.vectorBucket,
    );
    this.reportIndex = this.newVectorIndex(
      'ReportIndex',
      'report',
      this.vectorBucket,
    );
  }

  private newEvidenceBucket(bucketName: string): s3.Bucket {
    return new s3.Bucket(this, 'EvidenceBucket', {
      bucketName,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          id: 'expire-evidence-60d',
          prefix: 'rca/',
          expiration: cdk.Duration.days(60),
        },
      ],
    });
  }

  private newVectorBucket(vectorBucketName: string): s3vectors.CfnVectorBucket {
    return new s3vectors.CfnVectorBucket(this, 'VectorBucket', {
      vectorBucketName,
    });
  }

  private newVectorIndex(
    id: string,
    indexName: string,
    vectorBucket: s3vectors.CfnVectorBucket,
  ): s3vectors.CfnIndex {
    const index = new s3vectors.CfnIndex(this, id, {
      indexName,
      vectorBucketName: vectorBucket.vectorBucketName,
      dataType: 'float32',
      dimension: 1536,
      distanceMetric: 'cosine',
    });
    index.addDependency(vectorBucket);
    return index;
  }
}
