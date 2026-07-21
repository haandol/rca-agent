from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

from rca_agent.adapters.secondary.session import dynamodb_session_store
from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    DynamoDbSessionStore,
    build_idempotency_key,
    build_rca_id,
)
from rca_agent.adapters.secondary.trace import dynamodb_trace_store
from rca_agent.ports.dto.models import AlarmPayload, RcaSessionState
from rca_agent.services import evidence, playbook_gen, scoping
from rca_agent.services.pipeline import PipelineOrchestrator


class _StructuredAgent:
    def __init__(self, payload: dict):
        self._payload = payload

    def __call__(self, _prompt, *, structured_output_model):
        return SimpleNamespace(structured_output=structured_output_model.model_validate(self._payload))


class _FailingAgent:
    def __call__(self, _prompt, *, structured_output_model):  # noqa: ARG002
        raise RuntimeError("transient agent failure")


def _alarm_body() -> dict:
    return {
        "AlarmName": "HighCPU",
        "NewStateValue": "ALARM",
        "NewStateReason": "CPU crossed threshold",
        "Trigger": {
            "MetricName": "CPUUtilization",
            "Namespace": "AWS/ECS",
            "Dimensions": [],
        },
    }


def _create_table(ddb, table_name: str) -> None:
    ddb.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _session_item(ddb, table_name: str, rca_id: str) -> dict:
    return ddb.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": "strands#SESSION"},
        },
        ConsistentRead=True,
    )["Item"]


@mock_aws
def test_real_store_redelivery_reclaims_failed_session_and_completes(monkeypatch):
    table_name = "rca-sessions"
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    _create_table(ddb, table_name)
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", table_name)
    monkeypatch.setattr(dynamodb_trace_store, "DYNAMODB_TABLE_NAME", table_name)

    session_store = DynamoDbSessionStore(ddb)
    report_store = MagicMock()
    report_store.save.side_effect = lambda _report, *, claim_token, attempt: (
        f"reports/strands/attempt-{attempt}-{claim_token}/report.md"
    )
    notification = MagicMock()
    notification.send.return_value = True
    playbook_store = MagicMock()
    container = SimpleNamespace(
        session_store=session_store,
        dynamodb_client=ddb,
        report_store=report_store,
        notification=notification,
        playbook_store=playbook_store,
        s3_vectors_client=None,
        s3_client=MagicMock(),
        embedding=MagicMock(),
        scoping_agent=_StructuredAgent(
            {
                "alarm_summary": "CPU spike",
                "blast_radius": "single",
                "initial_severity": "high",
            }
        ),
        hypothesis_agent=_FailingAgent(),
        prioritization_agent=_FailingAgent(),
        evidence_mcp_clients=[],
        validation_agent=_StructuredAgent(
            {
                "judgment": {
                    "status": "CONFIRMED",
                    "confidence_score": 0.95,
                    "reasoning": "Evidence confirms the deployment.",
                }
            }
        ),
        branching_agent=_FailingAgent(),
        report_agent=_StructuredAgent(
            {
                "incident_summary": "CPU spike",
                "root_cause": "Bad deployment",
                "severity": "high",
            }
        ),
        playbook_agent=_StructuredAgent(
            {
                "failure_type": "deployment",
                "symptom_pattern": "CPU spike after deploy",
            }
        ),
    )
    orchestrator = PipelineOrchestrator(container)
    body = _alarm_body()
    alarm = AlarmPayload.from_cloudwatch_sns(body)
    rca_id = build_rca_id(build_idempotency_key(alarm))

    with (
        patch.object(scoping, "S3_VECTOR_BUCKET_NAME", ""),
        patch.object(playbook_gen, "S3_VECTOR_BUCKET_NAME", ""),
        patch.object(evidence, "S3_EVIDENCE_BUCKET", "evidence-bucket"),
        patch.object(
            evidence,
            "create_evidence_collection_agent",
            return_value=_StructuredAgent(
                {
                    "metrics_evidence": "CPU rose after the deployment.",
                    "combined_summary": "Deployment correlates with the spike.",
                }
            ),
        ),
    ):
        assert (
            orchestrator.process_alarm(
                body,
                receive_count=1,
                message_id="message-a",
            )
            is False
        )
        failed_item = _session_item(ddb, table_name, rca_id)
        first_claim = failed_item["claim_token"]["S"]
        assert failed_item["state"]["S"] == RcaSessionState.FAILED.value

        container.hypothesis_agent = _StructuredAgent(
            {
                "hypotheses": [
                    {
                        "title": f"Deployment hypothesis {index}",
                        "description": f"Deployment caused CPU spike {index}",
                        "category": "DEPLOYMENT",
                        "confidence_score": 0.8,
                    }
                    for index in range(3)
                ]
            }
        )

        assert (
            orchestrator.process_alarm(
                body,
                receive_count=2,
                message_id="message-a",
            )
            is True
        )

    completed_item = _session_item(ddb, table_name, rca_id)
    assert completed_item["state"]["S"] == RcaSessionState.COMPLETED.value
    assert completed_item["message_id"]["S"] == "message-a"
    assert completed_item["receive_count"]["N"] == "2"
    assert completed_item["claim_token"]["S"] != first_claim
    assert completed_item["completion_notification_status"]["S"] == "SENT"
    assert completed_item["claim_token"]["S"] in completed_item["report_s3_key"]["S"]
    report_store.save.assert_called_once()
    playbook_store.save.assert_called_once()
