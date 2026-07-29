from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import boto3
from moto import mock_aws

from rca_agent.adapters.secondary.session import dynamodb_session_store
from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    DynamoDbSessionStore,
    build_idempotency_key,
    build_rca_id,
)
from rca_agent.adapters.secondary.trace import dynamodb_trace_store
from rca_agent.ports.dto.models import (
    AlarmPayload,
    FaultType,
    NotificationMessage,
    RcaSessionState,
    RemediationResult,
)
from rca_agent.services import evidence, remediation_pipeline
from rca_agent.services.notification import build_notification
from rca_agent.services.pipeline import PipelineOrchestrator
from rca_agent.services.remediation_pipeline import RemediationOrchestrator


class _StructuredAgent:
    def __init__(self, payload: dict):
        self._payload = payload

    def __call__(self, _prompt, *, structured_output_model):
        return SimpleNamespace(
            structured_output=structured_output_model.model_validate(self._payload),
        )


class _FailingAgent:
    def __call__(self, _prompt, *, structured_output_model):  # noqa: ARG002
        raise RuntimeError("use deterministic fallback")


def _alarm_body() -> dict:
    return {
        "AlarmName": "Healthcare-HighCPU",
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


def _get_item(ddb, table_name: str, rca_id: str, sk: str) -> dict:
    return ddb.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": sk},
        },
        ConsistentRead=True,
    )["Item"]


@mock_aws
def test_validated_high_cpu_persists_and_is_the_only_remediation_type(
    monkeypatch,
):
    table_name = "rca-sessions"
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    _create_table(ddb, table_name)
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", table_name)
    monkeypatch.setattr(dynamodb_trace_store, "DYNAMODB_TABLE_NAME", table_name)

    session_store = DynamoDbSessionStore(ddb)
    notification = MagicMock()
    notification.send.return_value = True
    report_store = MagicMock()
    report_store.search_similar.return_value = []
    playbook_store = MagicMock()
    playbook_store.search_similar.return_value = []
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
                "alarm_summary": "Sustained CPU saturation",
                "blast_radius": "single",
                "initial_severity": "high",
            }
        ),
        hypothesis_agent=_StructuredAgent(
            {
                "hypotheses": [
                    {
                        "title": f"Initial database leak suggestion {index}",
                        "description": f"Database connection leak candidate {index}",
                        "category": "DEPENDENCY",
                        "confidence_score": 0.7,
                        "required_evidence": ["CPU metrics"],
                        "fault_type": "DB_CONNECTION_LEAK",
                    }
                    for index in range(3)
                ]
            }
        ),
        prioritization_agent=_FailingAgent(),
        evidence_mcp_clients=[],
        validation_agent=_StructuredAgent(
            {
                "judgment": {
                    "status": "CONFIRMED",
                    "confidence_score": 0.95,
                    "reasoning": "CPU evidence independently confirms saturation.",
                    "evidence_summary": ["CPUUtilization remained above 95%."],
                    "validated_fault_type": "HIGH_CPU",
                }
            }
        ),
        branching_agent=_FailingAgent(),
        report_agent=_StructuredAgent(
            {
                "incident_summary": "Sustained CPU saturation",
                "root_cause": "CPU saturation",
                "severity": "high",
            }
        ),
        playbook_agent=_StructuredAgent(
            {
                "failure_type": "high-cpu",
                "symptom_pattern": "CPUUtilization above 95%",
            }
        ),
        healthcare_service_host="healthcare.local",
        verification_agent=MagicMock(),
    )
    container.report_store.save.return_value = "reports/high-cpu.md"
    alarm = AlarmPayload.from_cloudwatch_sns(_alarm_body())
    rca_id = build_rca_id(build_idempotency_key(alarm))

    with (
        patch.object(evidence, "S3_EVIDENCE_BUCKET", ""),
        patch.object(
            evidence,
            "create_evidence_collection_agent",
            return_value=_StructuredAgent(
                {
                    "metrics_evidence": "CPUUtilization=98%",
                    "combined_summary": "CPUUtilization remained above 95%.",
                }
            ),
        ),
        patch(
            "rca_agent.services.pipeline.build_notification",
            wraps=build_notification,
        ) as notification_builder,
    ):
        assert PipelineOrchestrator(container).process_alarm(_alarm_body()) is True

    session_item = _get_item(ddb, table_name, rca_id, "strands#SESSION")
    selected_hypothesis_id = session_item["selected_hypothesis_id"]["S"]
    hypothesis_item = _get_item(
        ddb,
        table_name,
        rca_id,
        f"strands#HYPO#{selected_hypothesis_id}",
    )
    completion_notification = NotificationMessage.model_validate_json(
        session_item["completion_notification"]["S"],
    )

    assert hypothesis_item["fault_type"]["S"] == FaultType.DB_CONNECTION_LEAK.value
    assert hypothesis_item["validated_fault_type"]["S"] == FaultType.HIGH_CPU.value
    assert hypothesis_item["status"]["S"] == "CONFIRMED"
    assert hypothesis_item["evidence_summary"]["S"] == "CPUUtilization remained above 95%."
    assert hypothesis_item["validation_evidence_summary"]["S"] == "CPUUtilization remained above 95%."
    assert session_item["state"]["S"] == RcaSessionState.COMPLETED.value
    assert session_item["fault_type"]["S"] == FaultType.HIGH_CPU.value
    assert completion_notification.fault_type == FaultType.HIGH_CPU
    assert notification_builder.call_args.kwargs["fault_type"] == FaultType.HIGH_CPU

    context = session_store.get_remediation_context(rca_id)
    assert context is not None
    assert context.fault_type == FaultType.HIGH_CPU
    assert context.validated_fault_type == FaultType.HIGH_CPU
    assert context.validated_root_cause == hypothesis_item["description"]["S"]

    execute_remediation = Mock(
        return_value=RemediationResult(
            rca_id=rca_id,
            actions_taken=[],
            overall_success=False,
            summary="captured validated type",
        )
    )
    monkeypatch.setattr(
        remediation_pipeline,
        "execute_remediation",
        execute_remediation,
    )
    notification.reset_mock()

    RemediationOrchestrator(container).process_notification({"rca_id": rca_id})

    execute_remediation.assert_called_once()
    assert execute_remediation.call_args.kwargs["fault_type"] == FaultType.HIGH_CPU
    assert execute_remediation.call_args.kwargs["fault_type"] != FaultType.DB_CONNECTION_LEAK
