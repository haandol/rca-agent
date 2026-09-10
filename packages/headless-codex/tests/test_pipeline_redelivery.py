"""Exercise SQS delivery identity and real DynamoDB conditions without AWS/model calls."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from headless_codex.adapters.secondary.session import dynamodb_session_store
from headless_codex.adapters.secondary.session.dynamodb_session_store import DynamoDbSessionStore
from headless_codex.config.settings import ALARM_STALENESS_SECONDS
from headless_codex.ports.interfaces.session_store import SessionCancelledError
from headless_codex.services.pipeline import PipelineOrchestrator


@pytest.fixture
def delivery_runtime(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        table = "redelivery-sessions"
        ddb.create_table(
            TableName=table,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", table)
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue = sqs.create_queue(QueueName="alarm-redelivery", Attributes={"VisibilityTimeout": "0"})["QueueUrl"]
        store = DynamoDbSessionStore(ddb)
        orchestrator = PipelineOrchestrator(SimpleNamespace(session_store=store))

        def start_rca(rca_id, alarm_data, log, claim_token, *, attempt):
            store.update_state(rca_id, "SCOPING", claim_token=claim_token)
            return True

        run_rca = Mock(side_effect=start_rca)
        monkeypatch.setattr(orchestrator, "_run_rca", run_rca)
        yield SimpleNamespace(
            ddb=ddb, table=table, sqs=sqs, queue=queue, store=store, orchestrator=orchestrator, run_rca=run_rca
        )


def _send(runtime, at):
    return runtime.sqs.send_message(
        QueueUrl=runtime.queue,
        MessageBody=json.dumps(
            {
                "AlarmName": "HighCPU",
                "Region": "us-east-1",
                "NewStateValue": "ALARM",
                "StateChangeTime": at.isoformat(),
            }
        ),
    )["MessageId"]


def _receive(runtime):
    return runtime.sqs.receive_message(QueueUrl=runtime.queue, MessageSystemAttributeNames=["ApproximateReceiveCount"])[
        "Messages"
    ][0]


def _process(runtime, message):
    return runtime.orchestrator.process_message(
        message["Body"],
        receive_count=int(message["Attributes"]["ApproximateReceiveCount"]),
        message_id=message["MessageId"],
    )


def _items(runtime, sk):
    return [
        item
        for item in runtime.ddb.scan(TableName=runtime.table, ConsistentRead=True)["Items"]
        if item["SK"]["S"] == sk
    ]


def test_outdated_redelivery_does_not_open_incident_or_suppress_fresh_alarm(delivery_runtime):
    runtime = delivery_runtime
    stale_at = datetime.now(UTC) - timedelta(seconds=ALARM_STALENESS_SECONDS + 300)
    message_id = _send(runtime, stale_at)
    first = _receive(runtime)
    assert _process(runtime, first)
    before = _items(runtime, "ANALYSIS#SESSION")
    assert len(before) == 1
    assert before[0]["state"]["S"] == "OUTDATED"
    assert before[0]["message_id"]["S"] == message_id
    assert _items(runtime, "ACTIVE_INCIDENT") == []

    # The first acknowledgement is lost: SQS redelivers the already stored OUTDATED.
    second = _receive(runtime)
    assert second["MessageId"] == message_id
    assert second["Attributes"]["ApproximateReceiveCount"] == "2"
    assert _process(runtime, second)
    assert _items(runtime, "ACTIVE_INCIDENT") == []
    assert _items(runtime, "ANALYSIS#SESSION") == before
    runtime.run_rca.assert_not_called()
    runtime.sqs.delete_message(QueueUrl=runtime.queue, ReceiptHandle=second["ReceiptHandle"])

    _send(runtime, datetime.now(UTC))
    fresh = _receive(runtime)
    assert _process(runtime, fresh)
    runtime.run_rca.assert_called_once()
    active = _items(runtime, "ACTIVE_INCIDENT")
    assert len(active) == 1
    assert active[0]["candidate_rca_id"]["S"] == runtime.run_rca.call_args.args[0]


def test_stale_claim_failure_preserves_message_identity_for_reclaim(delivery_runtime, monkeypatch):
    runtime = delivery_runtime
    message_id = _send(runtime, datetime.now(UTC) - timedelta(seconds=ALARM_STALENESS_SECONDS + 300))
    first = _receive(runtime)
    with monkeypatch.context() as failed_write:
        failed_write.setattr(runtime.store, "mark_outdated", Mock(side_effect=RuntimeError("interrupted before write")))
        with pytest.raises(RuntimeError, match="interrupted before write"):
            _process(runtime, first)

    claimed = _items(runtime, "ANALYSIS#SESSION")[0]
    assert claimed["state"]["S"] == "ALARM_RECEIVED"
    assert claimed["message_id"]["S"] == message_id
    assert _items(runtime, "ACTIVE_INCIDENT") == []
    runtime.run_rca.assert_not_called()

    second = _receive(runtime)
    assert second["MessageId"] == message_id
    assert second["Attributes"]["ApproximateReceiveCount"] == "2"
    assert _process(runtime, second)
    runtime.run_rca.assert_called_once()
    reclaimed = _items(runtime, "ANALYSIS#SESSION")[0]
    assert reclaimed["state"]["S"] == "SCOPING"
    assert reclaimed["message_id"]["S"] == message_id
    assert reclaimed["claim_token"] != claimed["claim_token"]
    assert runtime.run_rca.call_args.kwargs["attempt"] == 2
    # Even if the interrupted first consumer wakes up, its token can no longer write.
    with pytest.raises(SessionCancelledError):
        runtime.store.mark_outdated(
            runtime.run_rca.call_args.args[0], "late outdated", claim_token=claimed["claim_token"]["S"]
        )
