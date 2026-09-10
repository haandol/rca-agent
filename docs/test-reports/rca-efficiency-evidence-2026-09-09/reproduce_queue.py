"""Offline audit probes; all AWS access is replaced by moto."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import boto3
from moto import mock_aws

from headless_codex.adapters.secondary.session import dynamodb_session_store as db
from headless_codex.services.pipeline import PipelineOrchestrator


def run_case(interrupt):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        table = "audit"
        ddb.create_table(
            TableName=table,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch.object(db, "DYNAMODB_TABLE_NAME", table):
            store = db.DynamoDbSessionStore(ddb)
            runner = PipelineOrchestrator(SimpleNamespace(session_store=store))
            runner._run_rca = Mock(return_value=True)
            old = {
                "AlarmName": "HighCPU",
                "NewStateValue": "ALARM",
                "Region": "us-east-1",
                "StateChangeTime": (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
                "Trigger": {"MetricName": "CPUUtilization", "Namespace": "AWS/ECS"},
            }
            if interrupt:
                with patch.object(store, "mark_outdated", side_effect=RuntimeError("simulated outage")):
                    try:
                        runner.process_message(json.dumps(old), message_id="sqs-1")
                    except RuntimeError:
                        pass
            else:
                assert runner.process_message(json.dumps(old), message_id="sqs-1")
            before = ddb.scan(TableName=table)["Items"]
            assert not any(x["SK"]["S"] == "ACTIVE_INCIDENT" for x in before)
            redelivery = runner.process_message(json.dumps(old), receive_count=2, message_id="sqs-1")
            after = ddb.scan(TableName=table)["Items"]
            new = {**old, "StateChangeTime": datetime.now(UTC).isoformat()}
            fresh = runner.process_message(json.dumps(new), message_id="sqs-2")
            session = next(x for x in before if x["SK"]["S"] == "ANALYSIS#SESSION")
            return {
                "case": "outdated_write_interrupted" if interrupt else "outdated_redelivery",
                "stored_message_id": session["message_id"]["S"],
                "old_state": session["state"]["S"],
                "redelivery_ack": redelivery,
                "active_incident_created_by_redelivery": any(x["SK"]["S"] == "ACTIVE_INCIDENT" for x in after),
                "new_alarm_ack": fresh,
                "analysis_calls": runner._run_rca.call_count,
            }


print(json.dumps([run_case(False), run_case(True)], indent=2))
