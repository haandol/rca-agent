"""Exercise the actual storage/runner/pipeline sequence with local AWS emulation, never live models."""

import json
import time
from unittest.mock import Mock

import boto3
import pytest
import structlog
from moto import mock_aws
from test_pipeline import ALARM_DATA, _container
from test_three_part_runtime import Scripted

from headless_codex.services.analysis_parts import AnalysisPartStore
from headless_codex.services.pipeline import PipelineOrchestrator


@pytest.fixture
def storage():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        s3 = boto3.client("s3", region_name="us-east-1")
        ddb.create_table(
            TableName="parts",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3.create_bucket(Bucket="parts-evidence")
        ddb.put_item(
            TableName="parts",
            Item={
                "PK": {"S": "RCA#rca"},
                "SK": {"S": "ANALYSIS#SESSION"},
                "state": {"S": "SCOPING"},
                "engine": {"S": "headless-codex"},
                "claim_token": {"S": "claim"},
                "ttl": {"N": str(int(time.time()) + 86400)},
            },
        )
        yield ddb, s3, AnalysisPartStore(ddb, s3, table_name="parts", bucket="parts-evidence", engine="headless-codex")


def _wire_real_publication_lease(container, ddb, monkeypatch):
    """Use the real lease store; emulate only the scripted specialist's Report state write."""
    from headless_codex.adapters.secondary.session import dynamodb_session_store as sessions

    monkeypatch.setattr(sessions, "DYNAMODB_TABLE_NAME", "parts")
    durable = sessions.DynamoDbSessionStore(ddb)
    run = container.codex_runner.run

    def finish_report(*args, **kwargs):
        result = run(*args, **kwargs)
        ddb.update_item(
            TableName="parts",
            Key={"PK": {"S": "RCA#rca"}, "SK": {"S": "ANALYSIS#SESSION"}},
            UpdateExpression="SET #state = :state",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":state": {"S": "REPORT_GENERATION"}},
        )
        return result

    container.codex_runner.run = finish_report
    container.session_store.acquire_side_effect_lease = Mock(wraps=durable.acquire_side_effect_lease)
    container.session_store.release_side_effect_lease = Mock(wraps=durable.release_side_effect_lease)
    container.session_store.mark_completion_notified = durable.mark_completion_notified
    container.session_store.mark_playbook_indexed = durable.mark_playbook_indexed
    return durable


def test_production_path_preserves_final_comparison_report_and_publication_after_three_parts(storage, monkeypatch):
    ddb, s3, parts = storage
    runner = Scripted()
    container = _container(runner)
    container.analysis_part_store = parts
    _wire_real_publication_lease(container, ddb, monkeypatch)
    container.observe_recovery = Mock(
        return_value={"context": None, "frozen_observations": {}, "verification": {"status": "UNAVAILABLE"}}
    )

    def publish(*args, **kwargs):
        assert parts.read_part("rca", "operations")["record"]["status"] == "COMPLETED"
        return True

    container.playbook_store.save_to_s3_vectors.side_effect = publish
    assert PipelineOrchestrator(container)._run_rca("rca", ALARM_DATA, structlog.get_logger(), "claim")
    parent = ddb.get_item(TableName="parts", Key={"PK": {"S": "RCA#rca"}, "SK": {"S": "ANALYSIS#SESSION"}})["Item"]
    assert parent["state"]["S"] == "COMPLETED"
    assert not any(name.startswith("side_effect_lease_") for name in parent)
    assert parent["completion_notification_status"]["S"] == "SENT"
    assert parent["playbook_index_status"]["S"] == "PUBLISHED"
    container.session_store.acquire_side_effect_lease.assert_called_once()
    container.session_store.release_side_effect_lease.assert_not_called()
    assert parent["report_s3_key"]["S"]
    assert json.loads(parent["completion_playbook"]["S"])["rca_id"] == "rca"
    container.playbook_store.search_similar.assert_called_once()
    container.playbook_store.save_to_s3_vectors.assert_called_once()
    report = container.report_store.save_report.call_args.args[1]
    assert "Analysis parts manifest" in report
    assert "## 정상화" in report and "## 근본원인·코드 수정" in report and "## 운영 개선" in report
    assert "5 Whys" in report and "Action Items" in report


def test_root_failure_still_persists_operations_report_without_public_runbook(storage, monkeypatch):
    ddb, s3, parts = storage
    runner = Scripted({"analysis-root-rca": 2})
    container = _container(runner)
    from headless_codex.adapters.secondary.session import dynamodb_session_store as sessions

    monkeypatch.setattr(sessions, "DYNAMODB_TABLE_NAME", "parts")
    durable = sessions.DynamoDbSessionStore(ddb)
    container.session_store.get_completion_handoff = durable.get_completion_handoff
    container.session_store.mark_completion_notified = durable.mark_completion_notified
    container.analysis_part_store = parts
    container.observe_recovery = Mock(
        return_value={"context": None, "frozen_observations": {}, "verification": {"status": "UNAVAILABLE"}}
    )
    assert PipelineOrchestrator(container)._run_rca("rca", ALARM_DATA, structlog.get_logger(), "claim")
    assert parts.read_part("rca", "root_cause")["record"]["status"] == "FAILED"
    assert parts.read_part("rca", "operations")["record"]["status"] == "COMPLETED"
    container.playbook_store.save_to_s3_vectors.assert_not_called()
    container.report_store.save_report.assert_called_once()
    parent = ddb.get_item(TableName="parts", Key={"PK": {"S": "RCA#rca"}, "SK": {"S": "ANALYSIS#SESSION"}})["Item"]
    assert parent["state"]["S"] == "FAILED"
    assert parent["analysis_parts_finalized"]["BOOL"] is True
    assert parent["completion_notification_status"]["S"] == "SENT"
    container.report_store.send_notification.assert_called_once()


def test_new_engine_claim_reads_original_incident_before_any_native_observation(storage, monkeypatch):
    ddb, s3, current = storage
    ddb.update_item(
        TableName="parts",
        Key={"PK": {"S": "RCA#rca"}, "SK": {"S": "ANALYSIS#SESSION"}},
        UpdateExpression="SET engine=:engine",
        ExpressionAttributeValues={":engine": {"S": "strands"}},
    )
    old = AnalysisPartStore(ddb, s3, table_name="parts", bucket="parts-evidence", engine="strands")
    original = {**ALARM_DATA, "AlarmName": "original failure", "NewStateReason": "before rollback"}
    frozen = old.freeze_incident(
        "rca", "claim", 1, {"alarm": original, "scoping": {}, "observations": {}, "source_artifacts": []}
    )
    ddb.update_item(
        TableName="parts",
        Key={"PK": {"S": "RCA#rca"}, "SK": {"S": "ANALYSIS#SESSION"}},
        UpdateExpression="SET engine=:engine",
        ExpressionAttributeValues={":engine": {"S": "headless-codex"}},
    )
    runner = Scripted()
    container = _container(runner)
    container.analysis_part_store = current
    _wire_real_publication_lease(container, ddb, monkeypatch)
    container.observe_recovery = Mock(side_effect=AssertionError("must not refreeze healthy service"))
    assert PipelineOrchestrator(container)._run_rca(
        "rca", {**ALARM_DATA, "AlarmName": "now healthy"}, structlog.get_logger(), "claim"
    )
    container.observe_recovery.assert_not_called()
    assert current.read_incident("rca") == frozen
    assert current.read_part("rca", "recovery")["payload"]["incident_ref"]["key"] == frozen["record"]["payload_s3_key"]


@pytest.mark.parametrize(
    "finalized,workflow", [(True, "recovery-first-v1"), (False, "recovery-first-v1"), (True, "other")]
)
def test_failed_handoff_native_conditions_and_retry(storage, monkeypatch, finalized, workflow):
    from headless_codex.adapters.secondary.session import dynamodb_session_store as sessions
    from headless_codex.ports.interfaces.session_store import ClaimDisposition

    ddb, _, _ = storage
    monkeypatch.setattr(sessions, "DYNAMODB_TABLE_NAME", "parts")
    store = sessions.DynamoDbSessionStore(ddb)
    key = {"PK": {"S": "RCA#rca"}, "SK": {"S": "ANALYSIS#SESSION"}}
    item = ddb.get_item(TableName="parts", Key=key)["Item"]
    item.update(
        {
            "state": {"S": "FAILED"},
            "workflow": {"S": workflow},
            "analysis_parts_finalized": {"BOOL": finalized},
            "completion_notification_status": {"S": "PENDING"},
            "completion_notification": {"S": json.dumps({"rca_id": "rca", "alarm_context": {}})},
        }
    )
    ddb.put_item(TableName="parts", Item=item)
    container = _container(Mock())
    container.session_store = store
    pipeline = PipelineOrchestrator(container)
    if finalized and workflow == "recovery-first-v1":
        claim = store.claim_session("rca", "alarm", "key", receive_count=2, message_id="message")
        assert claim.disposition == ClaimDisposition.TERMINAL_DUPLICATE
        assert claim.claim_token == "claim"
        container.report_store.send_notification.side_effect = [RuntimeError("retry notification"), None]
        assert not pipeline._flush_completion_handoff("rca", claim_token="claim", log=structlog.get_logger())
        assert store.get_completion_handoff("rca").notification_status == "PENDING"
        assert not store.mark_completion_notified("rca", claim_token="wrong")
        assert pipeline._flush_completion_handoff("rca", claim_token="claim", log=structlog.get_logger())
        assert pipeline._flush_completion_handoff("rca", claim_token="claim", log=structlog.get_logger())
        assert store.get_completion_handoff("rca").notification_status == "SENT"
        assert container.report_store.send_notification.call_count == 2
        container.playbook_store.save_to_s3_vectors.assert_not_called()
        container.codex_runner.run.assert_not_called()
    else:
        assert not pipeline._flush_completion_handoff("rca", claim_token="claim", log=structlog.get_logger())
        assert not store.mark_completion_notified("rca", claim_token="claim")
        container.report_store.send_notification.assert_not_called()


@pytest.mark.parametrize("stale", [False, True])
def test_finalized_failure_redelivery_flushes_without_incident_or_roles(stale):
    from test_pipeline import CLAIM_TOKEN, _past_staleness_boundary

    from headless_codex.ports.interfaces.session_store import ClaimDisposition, CompletionHandoff, SessionClaim

    container = _container(Mock())
    container.session_store.get_completion_handoff.return_value = CompletionHandoff(
        rca_id="rca",
        state="FAILED",
        workflow="recovery-first-v1",
        analysis_parts_finalized=True,
        notification_status="PENDING",
        notification={"alarm_name": "HighCPU"},
    )
    container.session_store.claim_session.return_value = SessionClaim(
        ClaimDisposition.TERMINAL_DUPLICATE, CLAIM_TOKEN, 1
    )
    container.report_store.send_notification.side_effect = [RuntimeError("SNS unavailable"), None]
    alarm = {**ALARM_DATA}
    if stale:
        alarm["StateChangeTime"] = _past_staleness_boundary()
    pipeline = PipelineOrchestrator(container)
    assert not pipeline.process_message(json.dumps(alarm), receive_count=2, message_id="same")
    assert pipeline.process_message(json.dumps(alarm), receive_count=3, message_id="same")
    container.session_store.claim_incident.assert_not_called()
    container.session_store.mark_completion_notified.assert_called_once()
    container.codex_runner.run.assert_not_called()
    container.playbook_store.save_to_s3_vectors.assert_not_called()


@pytest.mark.parametrize("mutation", ["replaced", "expired"])
def test_publication_does_not_complete_under_changed_or_expired_lease(storage, monkeypatch, mutation):
    ddb, _, parts = storage
    container = _container(Scripted())
    container.analysis_part_store = parts
    container.observe_recovery = Mock(
        return_value={"context": None, "frozen_observations": {}, "verification": {"status": "UNAVAILABLE"}}
    )
    _wire_real_publication_lease(container, ddb, monkeypatch)
    key = {"PK": {"S": "RCA#rca"}, "SK": {"S": "ANALYSIS#SESSION"}}

    def save_report(*args, **kwargs):
        parent = ddb.get_item(TableName="parts", Key=key)["Item"]
        assert parent["side_effect_lease_token"]["S"]
        field, value = (
            ("side_effect_lease_token", {"S": "another-owner"})
            if mutation == "replaced"
            else ("side_effect_lease_expires_at", {"N": "0"})
        )
        ddb.update_item(
            TableName="parts",
            Key=key,
            UpdateExpression=f"SET {field} = :value",
            ExpressionAttributeValues={":value": value},
        )
        return "reports/rca.md"

    container.report_store.save_report.side_effect = save_report
    assert not PipelineOrchestrator(container)._run_rca("rca", ALARM_DATA, structlog.get_logger(), "claim")
    parent = ddb.get_item(TableName="parts", Key=key)["Item"]
    assert parent["state"]["S"] != "COMPLETED"
    assert "analysis_parts_finalized" not in parent
    assert "completion_playbook" not in parent
    container.playbook_store.save_to_s3_vectors.assert_not_called()
    container.report_store.send_notification.assert_not_called()
    if mutation == "replaced":
        assert parent["side_effect_lease_token"]["S"] == "another-owner"
