import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';

/**
 * Lets the task's execution role pull container images from ECR.
 *
 * Every Fargate task definition in this repo builds from an ECR image, so the
 * grant is identical across stacks.
 */
export function grantEcrPull(taskDef: ecs.FargateTaskDefinition): void {
  taskDef.executionRole!.addManagedPolicy(
    iam.ManagedPolicy.fromAwsManagedPolicyName(
      'AmazonEC2ContainerRegistryReadOnly',
    ),
  );
}
