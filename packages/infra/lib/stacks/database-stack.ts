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

    /**
     * The session list, newest first, per engine.
     *
     * Listing sessions used to scan the whole table and throw most of it away:
     * spans and hypotheses outnumber sessions roughly seven to one, so the cost
     * and latency tracked total trace volume rather than session count, and a
     * scan returns no ordering — "the newest 25" could not be asked for.
     *
     * The keys are attributes only a session record carries. Reusing `engine` and
     * `created_at`, which hypotheses and executions also have, would pull those
     * into the index and a page of 25 would come back mostly hypotheses. Because
     * DynamoDB omits items missing an index key, writing these two only on
     * sessions makes the index session-only without a filter.
     *
     * The projection carries what one list row draws. Anything else — the steps
     * and executions that decide whether a report still awaits approval — is read
     * from the session's own partition, and only for the rows on the page.
     */
    table.addGlobalSecondaryIndex({
      indexName: 'session-by-engine-index',
      partitionKey: {
        name: 'list_engine',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: { name: 'list_created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: [
        'rca_id',
        'engine',
        'state',
        'alarm_name',
        'alarm_arn',
        'root_cause',
        'confirmed',
        'error_reason',
        'outdated_reason',
        'created_at',
        'updated_at',
      ],
    });

    return table;
  }
}
