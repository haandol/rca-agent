"""A finalized failed analysis is a durable outcome, not an unchanged job to execute forever."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rca_agent.adapters.secondary.session.dynamodb_session_store import build_idempotency_key, build_rca_id
from rca_agent.adapters.secondary.trace import dynamodb_trace_store
from rca_agent.ports.dto.models import AlarmPayload, NotificationMessage
from rca_agent.ports.interfaces.session_store import ClaimDisposition
from rca_agent.services.pipeline import PipelineOrchestrator

pytest_plugins = ["tests.test_session_store"]


def finalize_failure(store, ddb, table):
    """Represent the marker committed by the tested three-part transaction, retaining original claim ownership."""
    alarm = AlarmPayload(alarm_name="failed-parts", state_change_time=datetime.now(UTC))
    claim = store.claim_session(alarm, receive_count=1, message_id="message")
    rca_id = build_rca_id(build_idempotency_key(alarm))
    store.mark_failed(rca_id, error_reason="root failed", claim_token=claim.claim_token)
    notification = NotificationMessage(
        rca_id=rca_id, root_cause_summary="Root analysis failed", severity="high", confirmed=False
    )
    ddb.update_item(
        TableName=table,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": "ANALYSIS#SESSION"}},
        UpdateExpression=(
            "SET workflow = :workflow, analysis_parts_finalized = :yes, "
            "completion_notification = :body, completion_notification_status = :pending"
        ),
        ExpressionAttributeValues={
            ":workflow": {"S": "recovery-first-v1"},
            ":yes": {"BOOL": True},
            ":body": {"S": notification.model_dump_json()},
            ":pending": {"S": "PENDING"},
        },
    )
    return alarm, rca_id, claim.claim_token


def test_failed_parts_redelivery_retries_notification_only_then_acknowledges(claim_store):
    """A failed send retries its handoff, while successful delivery ACKs without replaying source or model work."""
    store, ddb, table = claim_store
    alarm, rca_id, token = finalize_failure(store, ddb, table)
    sender = Mock(send=Mock(side_effect=[False, True]))
    orchestrator = PipelineOrchestrator(SimpleNamespace(session_store=store, notification=sender))
    orchestrator._run_pipeline = Mock(side_effect=AssertionError("finalized parts must not rerun"))
    body = {
        "AlarmName": alarm.alarm_name,
        "StateChangeTime": alarm.state_change_time.isoformat(),
        "NewStateValue": "ALARM",
    }
    assert orchestrator.process_alarm(body, receive_count=2, message_id="message") is False
    assert store.get_completion_handoff(rca_id).notification_status == "PENDING"
    assert orchestrator.process_alarm(body, receive_count=3, message_id="message") is True
    handoff = store.get_completion_handoff(rca_id)
    assert handoff.state == "FAILED" and handoff.analysis_parts_finalized and handoff.claim_token == token
    assert handoff.notification_status == "SENT"
    assert orchestrator.process_alarm(body, receive_count=4, message_id="message") is True
    assert sender.send.call_count == 2
    orchestrator._run_pipeline.assert_not_called()


def test_plain_crash_failed_remains_reclaimable_and_cannot_mark_notification_sent(claim_store):
    """Only the atomic final marker settles failure; incomplete and legacy failures retain retry behavior."""
    store, _, _ = claim_store
    alarm = AlarmPayload(alarm_name="crash", state_change_time=datetime.now(UTC))
    first = store.claim_session(alarm, receive_count=1, message_id="message")
    rca_id = build_rca_id(build_idempotency_key(alarm))
    store.mark_failed(rca_id, error_reason="crash", claim_token=first.claim_token)
    assert not store.mark_completion_notified(rca_id, claim_token=first.claim_token)
    second = store.claim_session(alarm, receive_count=2, message_id="message")
    assert second.disposition is ClaimDisposition.CLAIMED and second.claim_token != first.claim_token


def test_finalized_failure_allows_only_existing_trace_end_under_same_claim(claim_store, monkeypatch):
    """The final notification span can close without permitting new work or old-claim writes after failure."""
    store, ddb, table = claim_store
    alarm = AlarmPayload(alarm_name="trace", state_change_time=datetime.now(UTC))
    claim = store.claim_session(alarm, receive_count=1, message_id="message")
    rca_id = build_rca_id(build_idempotency_key(alarm))
    monkeypatch.setattr(dynamodb_trace_store, "DYNAMODB_TABLE_NAME", table)
    trace = dynamodb_trace_store.TraceStore(rca_id, claim_token=claim.claim_token, dynamodb_client=ddb)
    span = trace.start_span(dynamodb_trace_store.SpanType.NOTIFICATION)
    store.mark_failed(rca_id, error_reason="root failed", claim_token=claim.claim_token)
    with pytest.raises(dynamodb_trace_store.SessionOwnershipCheckError):
        trace.end_span(span)
    ddb.update_item(
        TableName=table,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": "ANALYSIS#SESSION"}},
        UpdateExpression="SET workflow = :workflow, analysis_parts_finalized = :yes",
        ExpressionAttributeValues={":workflow": {"S": "recovery-first-v1"}, ":yes": {"BOOL": True}},
    )
    trace.end_span(span, output_summary="failure notification finalized")
    with pytest.raises(dynamodb_trace_store.SessionCancelledError):
        trace.check_cancelled()
