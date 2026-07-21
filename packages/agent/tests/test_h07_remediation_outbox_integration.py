from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import boto3
import pytest
from moto import mock_aws

from rca_agent.adapters.secondary.session import dynamodb_session_store
from rca_agent.adapters.secondary.session.dynamodb_session_store import DynamoDbSessionStore
from rca_agent.ports.dto.models import (
    FaultType,
    NotificationMessage,
    RemediationAction,
    RemediationResult,
    VerificationResult,
    VerificationStatus,
)
from rca_agent.services import remediation_pipeline
from rca_agent.services.remediation_pipeline import (
    RemediationOrchestrator,
    RemediationPublicationContendedError,
)

RCA_ID = "rca-h07"
TABLE_NAME = "rca-sessions"


def _create_table(ddb) -> None:
    ddb.create_table(
        TableName=TABLE_NAME,
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


def _session_key() -> dict:
    return {
        "PK": {"S": f"RCA#{RCA_ID}"},
        "SK": {"S": "strands#SESSION"},
    }


def _seed_confirmed_session(ddb) -> None:
    ddb.put_item(
        TableName=TABLE_NAME,
        Item={
            **_session_key(),
            "state": {"S": "COMPLETED"},
            "alarm_name": {"S": "Healthcare-HighCPU"},
            "region": {"S": "us-east-1"},
            "root_cause": {"S": "sustained CPU saturation"},
            "confirmed": {"BOOL": True},
            "selected_hypothesis_id": {"S": "h-confirmed"},
            "fault_type": {"S": FaultType.HIGH_CPU.value},
        },
    )
    ddb.put_item(
        TableName=TABLE_NAME,
        Item={
            "PK": {"S": f"RCA#{RCA_ID}"},
            "SK": {"S": "strands#HYPO#h-confirmed"},
            "status": {"S": "CONFIRMED"},
            "description": {"S": "sustained CPU saturation"},
            "evidence_summary": {"S": "CPUUtilization remained above 95%"},
            "validation_evidence_summary": {"S": "CPU saturation independently validated"},
            "validated_fault_type": {"S": FaultType.HIGH_CPU.value},
        },
    )


def _result() -> RemediationResult:
    return RemediationResult(
        rca_id=RCA_ID,
        actions_taken=[
            RemediationAction(
                action_type="fault_reset_api",
                description="reset high CPU fault",
                executed=True,
                success=True,
            )
        ],
        overall_success=True,
        summary="[SUCCESS] reset high CPU fault",
    )


def _verification() -> VerificationResult:
    return VerificationResult(
        rca_id=RCA_ID,
        status=VerificationStatus.NORMALIZED,
        verification_summary="CPUUtilization normalized",
    )


def _notification() -> dict:
    return {
        "rca_id": RCA_ID,
        "root_cause_summary": "untrusted delivery payload",
        "confirmed": False,
    }


def _container(store, notification):
    return SimpleNamespace(
        session_store=store,
        notification=notification,
        healthcare_service_host="healthcare.local",
        verification_agent=MagicMock(),
        cloudwatch_client_for_region=MagicMock(return_value=MagicMock()),
    )


def _item(ddb) -> dict:
    return ddb.get_item(
        TableName=TABLE_NAME,
        Key=_session_key(),
        ConsistentRead=True,
    )["Item"]


@mock_aws
def test_complete_precedes_publish_sent_mark_retries_and_duplicate_is_idempotent(
    monkeypatch,
):
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    _create_table(ddb)
    _seed_confirmed_session(ddb)
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", TABLE_NAME)
    monkeypatch.setattr(remediation_pipeline, "PUBLICATION_MARK_RETRY_DELAY_SECONDS", 0)

    store = DynamoDbSessionStore(ddb)
    reset = Mock(return_value=_result())
    verify = Mock(return_value=_verification())
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", reset)
    monkeypatch.setattr(remediation_pipeline, "run_verification", verify)

    published = []

    def publish(notification):
        item = _item(ddb)
        assert item["remediation_status"]["S"] == "COMPLETED"
        assert item["remediation_notification_status"]["S"] == "PUBLISHING"
        assert "remediation_claim_token" not in item
        published.append(notification)
        return True

    notifier = SimpleNamespace(send=Mock(side_effect=publish))
    original_mark = store.mark_remediation_published
    mark_attempts = 0

    def fail_first_sent_mark(rca_id, publication_claim_token):
        nonlocal mark_attempts
        mark_attempts += 1
        if mark_attempts == 1:
            return False
        return original_mark(rca_id, publication_claim_token)

    monkeypatch.setattr(store, "mark_remediation_published", fail_first_sent_mark)
    orchestrator = RemediationOrchestrator(_container(store, notifier))

    assert orchestrator.process_notification(_notification()) == _result()
    assert orchestrator.process_notification({**_notification(), "root_cause_summary": "changed raw event"}) is None

    item = _item(ddb)
    handoff = store.get_remediation_handoff(RCA_ID)
    assert item["remediation_notification_status"]["S"] == "SENT"
    assert "remediation_publication_claim_token" not in item
    assert handoff is not None
    assert handoff.result == _result()
    assert handoff.verification == _verification()
    assert handoff.notification == published[0]
    assert handoff.notification.publication_id
    assert reset.call_count == 1
    assert verify.call_count == 1
    assert notifier.send.call_count == 1
    assert mark_attempts == 2


@mock_aws
def test_publish_failure_releases_to_pending_and_duplicate_flushes_persisted_payload(
    monkeypatch,
):
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    _create_table(ddb)
    _seed_confirmed_session(ddb)
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", TABLE_NAME)

    store = DynamoDbSessionStore(ddb)
    reset = Mock(return_value=_result())
    verify = Mock(return_value=_verification())
    monkeypatch.setattr(remediation_pipeline, "execute_remediation", reset)
    monkeypatch.setattr(remediation_pipeline, "run_verification", verify)
    notifier = SimpleNamespace(send=Mock(side_effect=[False, True]))
    orchestrator = RemediationOrchestrator(_container(store, notifier))

    with pytest.raises(RuntimeError, match="Failed to publish remediation result"):
        orchestrator.process_notification(_notification())

    failed_item = _item(ddb)
    assert failed_item["remediation_status"]["S"] == "COMPLETED"
    assert failed_item["remediation_notification_status"]["S"] == "PENDING"
    assert "remediation_claim_token" not in failed_item

    assert orchestrator.process_notification({**_notification(), "root_cause_summary": "malicious replacement"}) is None

    sent_item = _item(ddb)
    persisted = NotificationMessage.model_validate_json(sent_item["remediation_notification"]["S"])
    first_attempt, retry_attempt = [call.args[0] for call in notifier.send.call_args_list]
    assert sent_item["remediation_notification_status"]["S"] == "SENT"
    assert first_attempt == persisted
    assert retry_attempt == persisted
    assert retry_attempt.root_cause_summary != "malicious replacement"
    assert reset.call_count == 1
    assert verify.call_count == 1
    assert notifier.send.call_count == 2


@mock_aws
def test_concurrent_publication_claim_has_one_winner(monkeypatch):
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    _create_table(ddb)
    notification = NotificationMessage(
        rca_id=RCA_ID,
        publication_id="publication-h07",
        root_cause_summary="persisted remediation result",
        severity="medium",
        event_type="remediation_complete",
    )
    ddb.put_item(
        TableName=TABLE_NAME,
        Item={
            **_session_key(),
            "state": {"S": "COMPLETED"},
            "confirmed": {"BOOL": True},
            "remediation_status": {"S": "COMPLETED"},
            "remediation_notification_status": {"S": "PENDING"},
            "remediation_notification": {"S": notification.model_dump_json()},
        },
    )
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", TABLE_NAME)
    barrier = Barrier(2)

    def claim() -> str | None:
        barrier.wait()
        return DynamoDbSessionStore(ddb).claim_remediation_publication(RCA_ID, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _index: claim(), range(2)))

    assert sum(token is not None for token in claims) == 1
    assert _item(ddb)["remediation_notification_status"]["S"] == "PUBLISHING"


@mock_aws
def test_contended_duplicate_defers_to_unacked_owner_then_expired_lease_is_reclaimed(
    monkeypatch,
):
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    _create_table(ddb)
    _seed_confirmed_session(ddb)
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", TABLE_NAME)
    store = DynamoDbSessionStore(ddb)
    result = _result()
    verification = _verification()
    notification = NotificationMessage(
        rca_id=RCA_ID,
        publication_id="publication-h07",
        root_cause_summary="persisted remediation result",
        severity="medium",
        verification_status=VerificationStatus.NORMALIZED,
        event_type="remediation_complete",
    )
    claim = store.claim_remediation(RCA_ID)
    assert claim
    assert store.complete_remediation(RCA_ID, claim, result, verification, notification)
    owner_claim = store.claim_remediation_publication(RCA_ID, lease_seconds=60)
    assert owner_claim

    notifier = SimpleNamespace(send=Mock(return_value=True))
    orchestrator = RemediationOrchestrator(_container(store, notifier))

    # The retryable error keeps this delivery unacked. Queue visibility is
    # 15 minutes, longer than the 60-second lease, so redelivery can reclaim.
    with pytest.raises(RemediationPublicationContendedError):
        orchestrator.process_notification(_notification())
    notifier.send.assert_not_called()

    ddb.update_item(
        TableName=TABLE_NAME,
        Key=_session_key(),
        UpdateExpression="SET remediation_publication_claim_expires_at = :expired",
        ExpressionAttributeValues={":expired": {"N": "0"}},
    )
    assert orchestrator.process_notification(_notification()) is None
    notifier.send.assert_called_once_with(notification)
    assert _item(ddb)["remediation_notification_status"]["S"] == "SENT"
