import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';

export class EcrStack extends cdk.Stack {
  readonly rcaAgentRepo: ecr.IRepository;
  readonly healthcareRepo: ecr.IRepository;
  readonly ccHeadlessRepo: ecr.IRepository;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const ns = this.node.tryGetContext('ns') as string;

    this.rcaAgentRepo = this.newRepository(`${ns.toLowerCase()}/rca-agent`);
    this.healthcareRepo = this.newRepository(`${ns.toLowerCase()}/healthcare`);
    this.ccHeadlessRepo = this.newRepository(`${ns.toLowerCase()}/cc-headless`);
  }

  private newRepository(name: string): ecr.Repository {
    return new ecr.Repository(this, name.replace(/\//g, '-'), {
      repositoryName: name,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [
        {
          maxImageCount: 10,
          description: 'Keep last 10 images',
        },
      ],
    });
  }
}
