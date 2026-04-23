import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

interface IProps extends cdk.StackProps {
  readonly rcaSessionTableName: string;
}

export class DatabaseStack extends cdk.Stack {
  readonly rcaSessionTable: dynamodb.ITable;

  constructor(scope: Construct, id: string, props: IProps) {
    super(scope, id, props);

    this.rcaSessionTable = this.newRcaSessionTable(props.rcaSessionTableName);
  }

  private newRcaSessionTable(tableName: string): dynamodb.Table {
    const table = new dynamodb.Table(this, 'RcaSessionTable', {
      tableName,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      timeToLiveAttribute: 'ttl',
    });

    table.addGlobalSecondaryIndex({
      indexName: 'idempotency-index',
      partitionKey: {
        name: 'idempotency_key',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    return table;
  }
}
