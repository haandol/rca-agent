import json

import boto3
import pytest
from moto import mock_aws

from cc_headless.adapters.secondary.session import dynamodb_session_store
from cc_headless.adapters.secondary.session.dynamodb_session_store import (
    DynamoDbSessionStore,
    SessionCancelledError,
)
from cc_headless.ports.interfaces.session_store import (
    ClaimDisposition,
    SessionClaim,
    SessionOwnershipCheckError,
    SideEffectLeaseUnavailableError,
)


@pytest.fixture
def session_store(monkeypatch):
    with mock_aws():
        table_name = "rca-sessions"
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=table_name,
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
        monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", table_name)
        yield DynamoDbSessionStore(ddb), ddb, table_name


def _claim(store: DynamoDbSessionStore, receive_count: int) -> SessionClaim:
    return store.claim_session(
        "rca-1",
        "HighCPU",
        "HighCPU#2026-07-21T00:00:00Z",
        receive_count=receive_count,
        alarm_data={"AlarmName": "HighCPU"},
    )


def _claim_token(store: DynamoDbSessionStore, receive_count: int) -> str:
    claim = _claim(store, receive_count)
    assert claim.acquired
    assert claim.claim_token
    return claim.claim_token


def _session_item(ddb, table_name: str) -> dict:
    return ddb.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": "RCA#rca-1"},
            "SK": {"S": "cc-headless#SESSION"},
        },
        ConsistentRead=True,
    )["Item"]


@pytest.mark.parametrize("state", ["ALARM_RECEIVED", "ANALYZING", "FAILED"])
def test_redelivery_reclaims_noncompleted_session(session_store, state):
    store, ddb, table_name = session_store
    first_claim = _claim_token(store, 1)

    if state == "ANALYZING":
        store.update_state("rca-1", "ANALYZING", claim_token=first_claim)
    elif state == "FAILED":
        store.mark_failed("rca-1", "first attempt failed", claim_token=first_claim)

    second = _claim(store, 2)

    assert second.acquired
    assert second.claim_token != first_claim
    item = _session_item(ddb, table_name)
    assert item["state"]["S"] == "ALARM_RECEIVED"
    assert item["receive_count"]["N"] == "2"
    assert item["claim_token"]["S"] == second.claim_token


def test_same_receive_count_cannot_steal_claim(session_store):
    store, ddb, table_name = session_store
    first_claim = _claim_token(store, 2)

    competing_claim = _claim(store, 2)

    assert competing_claim.disposition is ClaimDisposition.CONTENDED
    assert _session_item(ddb, table_name)["claim_token"]["S"] == first_claim


def test_lost_owner_is_cancelled_and_cannot_transition(session_store):
    store, _, _ = session_store
    first_claim = _claim_token(store, 1)
    store.update_state("rca-1", "ANALYZING", claim_token=first_claim)
    second_claim = _claim_token(store, 2)

    assert store.is_terminated("rca-1", claim_token=first_claim) is True
    assert store.is_terminated("rca-1", claim_token=second_claim) is False
    with pytest.raises(SessionCancelledError):
        store.update_state("rca-1", "ANALYZING", claim_token=first_claim)

    store.update_state("rca-1", "ANALYZING", claim_token=second_claim)


@pytest.mark.parametrize("terminal_state", ["COMPLETED", "OUTDATED", "CANCELLED"])
def test_terminal_session_is_never_reclaimed(session_store, terminal_state):
    store, _, _ = session_store
    claim_token = _claim_token(store, 1)

    if terminal_state == "COMPLETED":
        store.update_state("rca-1", "ANALYZING", claim_token=claim_token)
        store.mark_completed(
            "rca-1",
            "root cause",
            "reports/cc-headless/rca-1/report.md",
            claim_token=claim_token,
        )
    elif terminal_state == "OUTDATED":
        store.mark_outdated("rca-1", "stale alarm", claim_token=claim_token)
    else:
        store.update_state("rca-1", "CANCELLED", claim_token=claim_token)

    assert _claim(store, 2).disposition is ClaimDisposition.TERMINAL_DUPLICATE


def test_reclaim_uses_compare_and_swap_when_competing_writer_wins(session_store, monkeypatch):
    store, ddb, table_name = session_store
    _claim_token(store, 1)
    original_put_item = ddb.put_item

    def _racing_put_item(**kwargs):
        if kwargs.get("ConditionExpression", "").startswith("#state"):
            ddb.update_item(
                TableName=table_name,
                Key={
                    "PK": {"S": "RCA#rca-1"},
                    "SK": {"S": "cc-headless#SESSION"},
                },
                UpdateExpression="SET claim_token = :claim, receive_count = :count",
                ExpressionAttributeValues={
                    ":claim": {"S": "winning-claim"},
                    ":count": {"N": "2"},
                },
            )
        return original_put_item(**kwargs)

    monkeypatch.setattr(ddb, "put_item", _racing_put_item)

    assert _claim(store, 2).disposition is ClaimDisposition.CONTENDED


def test_legacy_nonterminal_session_without_claim_metadata_can_be_reclaimed(session_store):
    store, ddb, table_name = session_store
    ddb.put_item(
        TableName=table_name,
        Item={
            "PK": {"S": "RCA#rca-1"},
            "SK": {"S": "cc-headless#SESSION"},
            "state": {"S": "ANALYZING"},
        },
    )

    claim = _claim(store, 2)

    assert claim.acquired
    item = _session_item(ddb, table_name)
    assert item["state"]["S"] == "ALARM_RECEIVED"
    assert item["claim_token"]["S"] == claim.claim_token
    assert item["receive_count"]["N"] == "2"


def test_ownership_read_error_fails_closed(session_store, monkeypatch):
    store, _, _ = session_store
    claim_token = _claim_token(store, 1)
    store.update_state("rca-1", "ANALYZING", claim_token=claim_token)
    monkeypatch.setattr(store, "_get_session", lambda _rca_id: (_ for _ in ()).throw(RuntimeError("DDB down")))

    with pytest.raises(SessionOwnershipCheckError):
        store.is_terminated("rca-1", claim_token=claim_token)


def test_active_side_effect_lease_blocks_reclaim_until_release(session_store):
    store, _, _ = session_store
    first_claim = _claim_token(store, 1)
    store.update_state("rca-1", "ANALYZING", claim_token=first_claim)
    lease = store.acquire_side_effect_lease(
        "rca-1",
        claim_token=first_claim,
        effect_name="publish",
        lease_seconds=60,
    )

    assert _claim(store, 2).disposition is ClaimDisposition.CONTENDED

    store.release_side_effect_lease("rca-1", claim_token=first_claim, lease_token=lease)
    second_claim = _claim_token(store, 2)
    store.update_state("rca-1", "ANALYZING", claim_token=second_claim)

    with pytest.raises(SideEffectLeaseUnavailableError):
        store.acquire_side_effect_lease(
            "rca-1",
            claim_token=first_claim,
            effect_name="stale-publish",
            lease_seconds=60,
        )


@pytest.mark.parametrize(
    ("table_name", "client"),
    [
        ("", object()),
        ("rca-sessions", None),
    ],
)
def test_claim_and_side_effect_lease_fail_closed_without_dynamodb(monkeypatch, table_name, client):
    monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", table_name)
    store = DynamoDbSessionStore(client)

    claim = _claim(store, 1)

    assert claim.disposition is ClaimDisposition.CONTENDED
    assert claim.acquired is False
    with pytest.raises(SideEffectLeaseUnavailableError):
        store.acquire_side_effect_lease(
            "rca-1",
            claim_token="claim-token",
            effect_name="healthcare-reset",
            lease_seconds=60,
        )


def test_completion_atomically_persists_authoritative_report_key(session_store):
    store, ddb, table_name = session_store
    claim_token = _claim_token(store, 1)
    store.update_state("rca-1", "ANALYZING", claim_token=claim_token)
    lease_token = store.acquire_side_effect_lease(
        "rca-1",
        claim_token=claim_token,
        effect_name="final-publication",
        lease_seconds=60,
    )

    store.mark_completed(
        "rca-1",
        "database connection leak",
        "reports/cc-headless/rca-1/attempt-1/report.md",
        playbook={
            "playbook_id": "playbook-1",
            "verification_status": "DRAFT",
            "execution_steps": [{"step_id": "step-1"}],
        },
        confirmed=True,
        claim_token=claim_token,
        side_effect_lease_token=lease_token,
    )

    item = _session_item(ddb, table_name)
    assert item["state"]["S"] == "COMPLETED"
    assert item["root_cause"]["S"] == "database connection leak"
    assert item["report_s3_key"]["S"] == "reports/cc-headless/rca-1/attempt-1/report.md"
    assert item["playbook_id"]["S"] == "playbook-1"
    assert json.loads(item["playbook"]["S"])["execution_steps"] == [{"step_id": "step-1"}]
    assert item["confirmed"]["BOOL"] is True
    assert "side_effect_lease_token" not in item
