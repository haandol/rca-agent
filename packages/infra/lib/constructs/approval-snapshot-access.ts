import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';

const APPROVAL_SNAPSHOT_PATTERN = 'approvals/*';

/**
 * Approval snapshots are written by the dashboard before a request is queued.
 * Workers may read them, but no worker may change what a person approved.
 */
export function denyApprovalSnapshotMutation(
  role: iam.IRole,
  evidenceBucket: s3.IBucket,
): void {
  role.addToPrincipalPolicy(
    new iam.PolicyStatement({
      effect: iam.Effect.DENY,
      actions: ['s3:PutObject', 's3:DeleteObject'],
      resources: [evidenceBucket.arnForObjects(APPROVAL_SNAPSHOT_PATTERN)],
    }),
  );
}
