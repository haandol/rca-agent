import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { StorageStack } from '../lib/stacks/storage-stack';

function synthesizeStorage(): Template {
  const app = new cdk.App();
  const stack = new StorageStack(app, 'StorageTest', {
    evidenceBucketName: 'rca-test-evidence',
    vectorBucketName: 'rca-test-vectors',
  });
  return Template.fromStack(stack);
}

test('evidence bucket blocks public access and retains encrypted evidence', () => {
  const template = synthesizeStorage();

  template.hasResourceProperties('AWS::S3::Bucket', {
    BucketEncryption: {
      ServerSideEncryptionConfiguration: [
        {
          ServerSideEncryptionByDefault: {
            SSEAlgorithm: 'AES256',
          },
        },
      ],
    },
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
    LifecycleConfiguration: {
      Rules: Match.arrayWith([
        Match.objectLike({
          Id: 'expire-evidence-60d',
          Prefix: 'rca/',
          Status: 'Enabled',
        }),
      ]),
    },
  });
  template.hasResource('AWS::S3::Bucket', {
    DeletionPolicy: 'Retain',
    UpdateReplacePolicy: 'Retain',
  });
});

test('report, playbook, and evidence indexes share the embedding contract', () => {
  const template = synthesizeStorage();
  const indexes = template.findResources('AWS::S3Vectors::Index');

  expect(Object.values(indexes)).toHaveLength(3);
  expect(
    Object.values(indexes).map((resource) => resource.Properties.IndexName),
  ).toEqual(expect.arrayContaining(['evidence', 'playbook', 'report']));

  for (const resource of Object.values(indexes)) {
    expect(resource.Properties).toEqual(
      expect.objectContaining({
        DataType: 'float32',
        Dimension: 1536,
        DistanceMetric: 'cosine',
        VectorBucketName: 'rca-test-vectors',
      }),
    );
  }
});
