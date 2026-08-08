from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from rca_agent.adapters.secondary.session import dynamodb_session_store
from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    DynamoDbSessionStore,
    SessionCancelledError,
    build_active_incident_pk,
    build_alarm_identity,
    build_idempotency_key,
    build_rca_id,
    check_duplicate,
    create_session,
    mark_completed,
    mark_failed,
    update_state,
)
from rca_agent.ports.dto.models import (
    AlarmPayload,
    AlarmTrigger,
    FaultType,
    NotificationMessage,
    RcaSessionState,
)
from rca_agent.ports.interfaces.session_store import (
    ClaimDisposition,
    IncidentClaimDisposition,
    SessionOwnershipCheckError,
    SideEffectLeaseUnavailableError,
)

CLAIM_TOKEN = "claim-current"
MESSAGE_ID = "message-a"


@pytest.fixture()
def alarm() -> AlarmPayload:
    return AlarmPayload(
        alarm_name="HighCPU",
        alarm_arn="arn:aws:cloudwatch:us-east-1:123456789012:alarm:HighCPU",
        state_change_time=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
        trigger=AlarmTrigger(
            metric_name="CPUUtilization",
            namespace="AWS/ECS",
        ),
    )


@pytest.fixture()
def dynamodb_client() -> MagicMock:
    return MagicMock()


class TestBuildIdempotencyKey:
    def test_builds_key_from_alarm(self, alarm: AlarmPayload):
        key = build_idempotency_key(alarm)
        assert key == "HighCPU#2025-06-01T12:00:00+00:00"

    def test_builds_key_without_timestamp(self):
        alarm = AlarmPayload(alarm_name="NoTimestamp")
        key = build_idempotency_key(alarm)
        assert key == "NoTimestamp#unknown"


class TestBuildRcaId:
    def test_deterministic(self):
        key = "HighCPU#2025-06-01T12:00:00+00:00"
        id1 = build_rca_id(key)
        id2 = build_rca_id(key)
        assert id1 == id2

    def test_returns_valid_uuid(self):
        rca_id = build_rca_id("some-key")
        parsed = uuid.UUID(rca_id)
        assert parsed.version == 5

    def test_different_keys_produce_different_ids(self):
        id1 = build_rca_id("key-a")
        id2 = build_rca_id("key-b")
        assert id1 != id2

    def test_uses_namespace_url(self):
        key = "test-key"
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        assert build_rca_id(key) == expected


@pytest.fixture()
def claim_store(monkeypatch):
    with mock_aws():
        table_name = "rca-sessions"
        ddb = boto3.client("dynamodb", region_name="us-east-1")
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
        monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", table_name)
        yield DynamoDbSessionStore(ddb), ddb, table_name


def _claim(
    store: DynamoDbSessionStore,
    alarm: AlarmPayload,
    receive_count: int,
    message_id: str = MESSAGE_ID,
):
    return store.claim_session(
        alarm,
        receive_count=receive_count,
        message_id=message_id,
    )


def _claimed_token(store: DynamoDbSessionStore, alarm: AlarmPayload, receive_count: int) -> str:
    claim = _claim(store, alarm, receive_count)
    assert claim.acquired
    assert claim.claim_token
    return claim.claim_token


def _claimed_item(ddb, table_name: str, alarm: AlarmPayload) -> dict:
    rca_id = build_rca_id(build_idempotency_key(alarm))
    return ddb.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": "strands#SESSION"},
        },
        ConsistentRead=True,
    )["Item"]


def _advance_to(
    store: DynamoDbSessionStore,
    rca_id: str,
    claim_token: str,
    target: RcaSessionState,
) -> None:
    path = [
        RcaSessionState.SCOPING,
        RcaSessionState.HYPOTHESIS_GENERATION,
        RcaSessionState.HYPOTHESIS_PRIORITIZATION,
        RcaSessionState.EVIDENCE_COLLECTION,
        RcaSessionState.HYPOTHESIS_VALIDATION,
        RcaSessionState.REPORT_GENERATION,
    ]
    for state in path:
        assert store.update_state(rca_id, state, claim_token=claim_token)
        if state is target:
            return


def _alarm_at(alarm: AlarmPayload, moment: datetime, *, state: str = "ALARM") -> AlarmPayload:
    return alarm.model_copy(update={"state_change_time": moment, "new_state": state})


def _incident_item(ddb, table_name: str, alarm: AlarmPayload) -> dict:
    return ddb.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": build_active_incident_pk(alarm)},
            "SK": {"S": "ACTIVE_INCIDENT"},
        },
        ConsistentRead=True,
    )["Item"]


class TestActiveIncident:
    def test_uses_alarm_arn_or_stable_name_fallback_for_identity(self, alarm):
        assert build_alarm_identity(alarm) == alarm.alarm_arn
        without_arn = alarm.model_copy(update={"alarm_arn": None})
        assert build_alarm_identity(without_arn) == "cloudwatch:us-east-1:alarm:HighCPU"
        assert build_active_incident_pk(alarm).startswith("ALARM#")
        assert len(build_active_incident_pk(alarm)) == len("ALARM#") + 64

    def test_same_event_is_allowed_for_each_engine_session(self, claim_store, alarm):
        store, ddb, table_name = claim_store

        first = store.claim_incident(alarm, cooldown_seconds=300)
        second = store.claim_incident(alarm, cooldown_seconds=300)

        assert first.disposition is IncidentClaimDisposition.PROCEED
        assert second.disposition is IncidentClaimDisposition.PROCEED
        assert first.candidate_rca_id == second.candidate_rca_id
        assert first.generation == second.generation == 1
        ddb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"RCA#{first.candidate_rca_id}"},
                "SK": {"S": "cc-headless#SESSION"},
                "state": {"S": "ALARM_RECEIVED"},
            },
        )
        assert _claim(store, alarm, 1).acquired

    def test_newer_alarm_is_deferred_while_analysis_is_active(self, claim_store, alarm):
        store, _, _ = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        assert opened.acquired
        assert _claim(store, alarm, 1).acquired
        realarm = _alarm_at(alarm, alarm.state_change_time + timedelta(minutes=18))

        claim = store.claim_incident(realarm, cooldown_seconds=300)

        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.candidate_rca_id == opened.candidate_rca_id
        assert "strands#SESSION" in claim.reason
        assert claim.retryable

    def test_newer_alarm_is_deferred_while_execution_is_active(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        ddb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"RCA#{opened.candidate_rca_id}"},
                "SK": {"S": "EXEC_ACTIVE"},
                "execution_id": {"S": "exec-1"},
            },
        )
        realarm = _alarm_at(alarm, alarm.state_change_time + timedelta(minutes=18))

        claim = store.claim_incident(realarm, cooldown_seconds=300)

        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.reason == "EXEC_ACTIVE exists"
        assert claim.retryable

    def test_ok_immediate_realarm_is_suppressed_during_cooldown(self, claim_store, alarm):
        store, _, _ = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        assert store.record_recovery(_alarm_at(alarm, ok_at, state="OK"))

        realarm = _alarm_at(alarm, ok_at + timedelta(seconds=299))
        claim = store.claim_incident(realarm, cooldown_seconds=300)

        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.candidate_rca_id == opened.candidate_rca_id
        assert claim.reason == "recovery cooldown has not elapsed"
        assert not claim.retryable

    def test_ok_before_alarm_persists_watermark_and_applies_event_time_cooldown(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        ok_at = alarm.state_change_time
        recovery = _alarm_at(alarm, ok_at, state="OK")

        assert store.record_recovery(recovery)
        watermark = _incident_item(ddb, table_name, alarm)
        assert watermark["last_ok_at"]["S"] == ok_at.isoformat()
        assert "candidate_rca_id" not in watermark
        assert "generation" not in watermark

        during_cooldown = _alarm_at(alarm, ok_at + timedelta(seconds=299))
        suppressed = store.claim_incident(during_cooldown, cooldown_seconds=300)
        assert suppressed.disposition is IncidentClaimDisposition.SUPPRESSED
        assert suppressed.reason == "recovery cooldown has not elapsed"
        assert not suppressed.retryable

        at_boundary = _alarm_at(alarm, ok_at + timedelta(seconds=300))
        opened = store.claim_incident(at_boundary, cooldown_seconds=300)
        assert opened.disposition is IncidentClaimDisposition.PROCEED
        assert opened.generation == 1
        item = _incident_item(ddb, table_name, alarm)
        assert item["candidate_rca_id"]["S"] == opened.candidate_rca_id
        assert item["opened_at"]["S"] == at_boundary.state_change_time.isoformat()
        assert "last_ok_at" not in item

    def test_terminal_incident_without_ok_defers_newer_alarm(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        assert _claim(store, alarm, 1).acquired
        ddb.update_item(
            TableName=table_name,
            Key=_session_key_for_test(opened.candidate_rca_id),
            UpdateExpression="SET #state = :completed",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":completed": {"S": "COMPLETED"}},
        )

        realarm = _alarm_at(alarm, alarm.state_change_time + timedelta(minutes=18))
        pending = store.claim_incident(realarm, cooldown_seconds=300)

        assert pending.disposition is IncidentClaimDisposition.SUPPRESSED
        assert pending.candidate_rca_id == opened.candidate_rca_id
        assert pending.reason == "incident has no recovery observation"
        assert pending.retryable

    def test_alarm_before_ok_proceeds_when_recovery_arrives(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        assert _claim(store, alarm, 1).acquired
        ddb.update_item(
            TableName=table_name,
            Key=_session_key_for_test(opened.candidate_rca_id),
            UpdateExpression="SET #state = :completed",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":completed": {"S": "COMPLETED"}},
        )
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        realarm = _alarm_at(alarm, ok_at + timedelta(seconds=300))

        pending = store.claim_incident(realarm, cooldown_seconds=300)
        assert pending.retryable

        assert store.record_recovery(_alarm_at(alarm, ok_at, state="OK"))
        claim = store.claim_incident(realarm, cooldown_seconds=300)

        assert claim.disposition is IncidentClaimDisposition.PROCEED
        assert claim.generation == 2
        assert claim.candidate_rca_id != opened.candidate_rca_id
        assert store.claim_incident(realarm, cooldown_seconds=300).candidate_rca_id == claim.candidate_rca_id

    def test_alarm_before_ok_while_active_defers_until_recovery_and_terminal_state(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        assert _claim(store, alarm, 1).acquired
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        realarm = _alarm_at(alarm, ok_at + timedelta(seconds=300))

        before_ok = store.claim_incident(realarm, cooldown_seconds=300)
        assert before_ok.disposition is IncidentClaimDisposition.SUPPRESSED
        assert before_ok.retryable
        assert "strands#SESSION" in before_ok.reason

        assert store.record_recovery(_alarm_at(alarm, ok_at, state="OK"))
        before_terminal = store.claim_incident(realarm, cooldown_seconds=300)
        assert before_terminal.disposition is IncidentClaimDisposition.SUPPRESSED
        assert before_terminal.retryable
        assert "recovery observed but" in before_terminal.reason

        ddb.update_item(
            TableName=table_name,
            Key=_session_key_for_test(opened.candidate_rca_id),
            UpdateExpression="SET #state = :completed",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":completed": {"S": "COMPLETED"}},
        )
        claim = store.claim_incident(realarm, cooldown_seconds=300)

        assert claim.disposition is IncidentClaimDisposition.PROCEED
        assert claim.generation == 2

    def test_recovery_older_than_current_generation_is_ignored(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        store.claim_incident(alarm, cooldown_seconds=300)

        older_ok = alarm.state_change_time - timedelta(minutes=1)
        assert store.record_recovery(_alarm_at(alarm, older_ok, state="OK"))

        item = _incident_item(ddb, table_name, alarm)
        assert "last_ok_at" not in item

    def test_terminal_sessions_after_cooldown_allow_new_generation(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        assert _claim(store, alarm, 1).acquired
        ddb.update_item(
            TableName=table_name,
            Key=_session_key_for_test(opened.candidate_rca_id),
            UpdateExpression="SET #state = :completed",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":completed": {"S": "COMPLETED"}},
        )
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        assert store.record_recovery(_alarm_at(alarm, ok_at, state="OK"))
        realarm = _alarm_at(alarm, ok_at + timedelta(seconds=301))

        claim = store.claim_incident(realarm, cooldown_seconds=300)

        assert claim.disposition is IncidentClaimDisposition.PROCEED
        assert claim.generation == 2
        assert claim.candidate_rca_id != opened.candidate_rca_id
        item = _incident_item(ddb, table_name, alarm)
        assert item["candidate_rca_id"]["S"] == claim.candidate_rca_id
        assert item["generation"]["N"] == "2"
        assert "last_ok_at" not in item

    def test_generation_condition_conflict_preserves_winner(self, claim_store, alarm, monkeypatch):
        store, ddb, table_name = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        assert store.record_recovery(_alarm_at(alarm, ok_at, state="OK"))
        contender = _alarm_at(alarm, ok_at + timedelta(seconds=301))
        winner_alarm = _alarm_at(alarm, ok_at + timedelta(seconds=302))
        winner_rca_id = build_rca_id(build_idempotency_key(winner_alarm))
        original_transact = ddb.transact_write_items
        injected = False

        def racing_transact(**kwargs):
            nonlocal injected
            if not injected:
                injected = True
                ddb.update_item(
                    TableName=table_name,
                    Key={
                        "PK": {"S": build_active_incident_pk(alarm)},
                        "SK": {"S": "ACTIVE_INCIDENT"},
                    },
                    UpdateExpression=(
                        "SET candidate_rca_id = :candidate, generation = :generation, "
                        "opened_at = :opened, last_alarm_at = :alarm REMOVE last_ok_at"
                    ),
                    ExpressionAttributeValues={
                        ":candidate": {"S": winner_rca_id},
                        ":generation": {"N": "2"},
                        ":opened": {"S": winner_alarm.state_change_time.isoformat()},
                        ":alarm": {"S": winner_alarm.state_change_time.isoformat()},
                    },
                )
            return original_transact(**kwargs)

        monkeypatch.setattr(ddb, "transact_write_items", racing_transact)

        claim = store.claim_incident(contender, cooldown_seconds=300)

        assert injected
        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.candidate_rca_id == winner_rca_id
        assert claim.candidate_rca_id != opened.candidate_rca_id
        assert _incident_item(ddb, table_name, alarm)["candidate_rca_id"]["S"] == winner_rca_id

    def test_same_candidate_touch_retries_after_concurrent_generation_promotion(
        self,
        claim_store,
        alarm,
        monkeypatch,
    ):
        store, ddb, table_name = claim_store
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        winner_alarm = _alarm_at(alarm, alarm.state_change_time + timedelta(minutes=20))
        winner_rca_id = build_rca_id(build_idempotency_key(winner_alarm))
        original_update = ddb.update_item
        promoted = False

        def promote_before_touch(**kwargs):
            nonlocal promoted
            if not promoted and "last_alarm_at" in kwargs.get("UpdateExpression", ""):
                promoted = True
                original_update(
                    TableName=table_name,
                    Key={
                        "PK": {"S": build_active_incident_pk(alarm)},
                        "SK": {"S": "ACTIVE_INCIDENT"},
                    },
                    UpdateExpression=(
                        "SET candidate_rca_id = :candidate, generation = :generation, "
                        "opened_at = :opened, last_alarm_at = :alarm"
                    ),
                    ExpressionAttributeValues={
                        ":candidate": {"S": winner_rca_id},
                        ":generation": {"N": "2"},
                        ":opened": {"S": winner_alarm.state_change_time.isoformat()},
                        ":alarm": {"S": winner_alarm.state_change_time.isoformat()},
                    },
                )
            return original_update(**kwargs)

        monkeypatch.setattr(ddb, "update_item", promote_before_touch)

        claim = store.claim_incident(alarm, cooldown_seconds=300)

        assert promoted
        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.candidate_rca_id == winner_rca_id
        assert claim.candidate_rca_id != opened.candidate_rca_id
        item = _incident_item(ddb, table_name, alarm)
        assert item["candidate_rca_id"]["S"] == winner_rca_id
        assert item["generation"]["N"] == "2"

    def test_incident_gate_preserves_existing_session_redelivery_semantics(self, claim_store, alarm):
        store, _, _ = claim_store
        assert store.claim_incident(alarm, cooldown_seconds=300).acquired
        first = _claim(store, alarm, 1)
        assert first.acquired

        assert store.claim_incident(alarm, cooldown_seconds=300).acquired
        assert _claim(store, alarm, 1).disposition is ClaimDisposition.CONTENDED
        assert _claim(store, alarm, 2).acquired


def _session_key_for_test(rca_id: str) -> dict:
    return {
        "PK": {"S": f"RCA#{rca_id}"},
        "SK": {"S": "strands#SESSION"},
    }


class TestSessionClaim:
    def test_first_delivery_claims_and_persists_metadata(self, claim_store, alarm):
        store, ddb, table_name = claim_store

        claim = _claim(store, alarm, 1)

        assert claim.disposition is ClaimDisposition.CLAIMED
        assert claim.attempt == 1
        assert claim.claim_token
        item = _claimed_item(ddb, table_name, alarm)
        assert item["claim_token"]["S"] == claim.claim_token
        assert item["receive_count"]["N"] == "1"
        assert item["message_id"]["S"] == MESSAGE_ID

    def test_first_delivery_persists_complete_raw_alarm_context(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        alarm_data = {
            "AlarmName": "HighCPU",
            "AlarmArn": alarm.alarm_arn,
            "NewStateValue": "ALARM",
            "NewStateReason": "Threshold crossed",
            "StateChangeTime": "2025-06-01T12:00:00Z",
            "Trigger": {
                "MetricName": "CPUUtilization",
                "Namespace": "AWS/ECS",
                "Dimensions": [
                    {"name": "ClusterName", "value": "prod"},
                    {"name": "ServiceName", "value": "web"},
                ],
                "Statistic": "Average",
                "Period": 60,
                "Threshold": 80,
                "ComparisonOperator": "GreaterThanThreshold",
            },
        }

        claim = store.claim_session(
            alarm,
            receive_count=1,
            message_id=MESSAGE_ID,
            alarm_data=alarm_data,
        )

        assert claim.acquired
        item = _claimed_item(ddb, table_name, alarm)
        assert json.loads(item["alarm_data"]["S"]) == alarm_data

    @pytest.mark.parametrize(
        "state",
        [
            RcaSessionState.ALARM_RECEIVED,
            RcaSessionState.SCOPING,
            RcaSessionState.HYPOTHESIS_GENERATION,
            RcaSessionState.HYPOTHESIS_PRIORITIZATION,
            RcaSessionState.EVIDENCE_COLLECTION,
            RcaSessionState.HYPOTHESIS_VALIDATION,
            RcaSessionState.REPORT_GENERATION,
            RcaSessionState.FAILED,
        ],
    )
    def test_larger_receive_count_reclaims_nonterminal_state(self, claim_store, alarm, state):
        store, ddb, table_name = claim_store
        rca_id = build_rca_id(build_idempotency_key(alarm))
        first_token = _claimed_token(store, alarm, 1)
        if state is RcaSessionState.FAILED:
            assert store.mark_failed(rca_id, error_reason="transient", claim_token=first_token)
        elif state is not RcaSessionState.ALARM_RECEIVED:
            _advance_to(store, rca_id, first_token, state)

        second = _claim(store, alarm, 2)

        assert second.acquired
        assert second.claim_token != first_token
        assert second.attempt == 2
        item = _claimed_item(ddb, table_name, alarm)
        assert item["state"]["S"] == RcaSessionState.ALARM_RECEIVED.value
        assert item["receive_count"]["N"] == "2"
        assert item["claim_token"]["S"] == second.claim_token

    def test_same_or_smaller_receive_count_cannot_steal_claim(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        first_token = _claimed_token(store, alarm, 2)

        assert _claim(store, alarm, 2).disposition is ClaimDisposition.CONTENDED
        assert _claim(store, alarm, 1).disposition is ClaimDisposition.CONTENDED
        assert _claimed_item(ddb, table_name, alarm)["claim_token"]["S"] == first_token

    def test_different_messages_cannot_alternate_reclaims(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        first = _claim(store, alarm, 1, "message-a")
        assert first.acquired

        assert _claim(store, alarm, 99, "message-b").disposition is ClaimDisposition.CONTENDED
        second = _claim(store, alarm, 2, "message-a")
        assert second.acquired
        assert second.claim_token != first.claim_token
        assert _claim(store, alarm, 100, "message-b").disposition is ClaimDisposition.CONTENDED

        item = _claimed_item(ddb, table_name, alarm)
        assert item["message_id"]["S"] == "message-a"
        assert item["receive_count"]["N"] == "2"
        assert item["claim_token"]["S"] == second.claim_token

    @pytest.mark.parametrize(
        "terminal_state",
        [
            RcaSessionState.COMPLETED,
            RcaSessionState.OUTDATED,
            RcaSessionState.CANCELLED,
        ],
    )
    def test_terminal_session_is_duplicate(self, claim_store, alarm, terminal_state):
        store, _, _ = claim_store
        rca_id = build_rca_id(build_idempotency_key(alarm))
        token = _claimed_token(store, alarm, 1)
        if terminal_state is RcaSessionState.COMPLETED:
            _advance_to(store, rca_id, token, RcaSessionState.REPORT_GENERATION)
            assert store.mark_completed(rca_id, claim_token=token)
        elif terminal_state is RcaSessionState.OUTDATED:
            assert store.mark_outdated(rca_id, reason="stale", claim_token=token)
        else:
            assert store.update_state(rca_id, RcaSessionState.CANCELLED, claim_token=token)

        duplicate = _claim(store, alarm, 2)

        assert duplicate.disposition is ClaimDisposition.TERMINAL_DUPLICATE
        assert duplicate.claim_token == token

    def test_reclaim_compare_and_swap_loses_to_competing_writer(
        self,
        claim_store,
        alarm,
        monkeypatch,
    ):
        store, ddb, table_name = claim_store
        _claimed_token(store, alarm, 1)
        rca_id = build_rca_id(build_idempotency_key(alarm))
        original_put_item = ddb.put_item

        def racing_put_item(**kwargs):
            if kwargs.get("ConditionExpression", "").startswith("#st"):
                ddb.update_item(
                    TableName=table_name,
                    Key={
                        "PK": {"S": f"RCA#{rca_id}"},
                        "SK": {"S": "strands#SESSION"},
                    },
                    UpdateExpression="SET claim_token = :claim, receive_count = :count",
                    ExpressionAttributeValues={
                        ":claim": {"S": "winning-claim"},
                        ":count": {"N": "2"},
                    },
                )
            return original_put_item(**kwargs)

        monkeypatch.setattr(ddb, "put_item", racing_put_item)

        assert _claim(store, alarm, 2).disposition is ClaimDisposition.CONTENDED
        assert _claimed_item(ddb, table_name, alarm)["claim_token"]["S"] == "winning-claim"

    def test_legacy_nonterminal_item_can_be_reclaimed(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        rca_id = build_rca_id(build_idempotency_key(alarm))
        ddb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": "strands#SESSION"},
                "state": {"S": RcaSessionState.SCOPING.value},
            },
        )

        claim = _claim(store, alarm, 2)

        assert claim.acquired
        item = _claimed_item(ddb, table_name, alarm)
        assert item["state"]["S"] == RcaSessionState.ALARM_RECEIVED.value
        assert item["receive_count"]["N"] == "2"
        assert item["claim_token"]["S"] == claim.claim_token
        assert item["message_id"]["S"] == MESSAGE_ID

    def test_active_side_effect_lease_blocks_reclaim_until_release(
        self,
        claim_store,
        alarm,
    ):
        store, _, _ = claim_store
        first_token = _claimed_token(store, alarm, 1)
        lease_token = store.acquire_side_effect_lease(
            build_rca_id(build_idempotency_key(alarm)),
            first_token,
            "playbook",
            lease_seconds=60,
        )

        assert _claim(store, alarm, 2).disposition is ClaimDisposition.CONTENDED
        assert store.release_side_effect_lease(
            build_rca_id(build_idempotency_key(alarm)),
            first_token,
            lease_token,
        )
        assert _claim(store, alarm, 2).acquired

    def test_stale_claim_cannot_acquire_or_release_side_effect_lease(
        self,
        claim_store,
        alarm,
    ):
        store, _, _ = claim_store
        rca_id = build_rca_id(build_idempotency_key(alarm))
        first_token = _claimed_token(store, alarm, 1)
        second_token = _claimed_token(store, alarm, 2)

        with pytest.raises(SideEffectLeaseUnavailableError):
            store.acquire_side_effect_lease(
                rca_id,
                first_token,
                "evidence:h-1",
                lease_seconds=60,
            )
        current_lease = store.acquire_side_effect_lease(
            rca_id,
            second_token,
            "evidence:h-1",
            lease_seconds=60,
        )
        assert not store.release_side_effect_lease(
            rca_id,
            first_token,
            current_lease,
        )

    def test_claim_fails_closed_without_store(self, alarm, monkeypatch):
        monkeypatch.setattr(dynamodb_session_store, "DYNAMODB_TABLE_NAME", "")

        claim = DynamoDbSessionStore(MagicMock()).claim_session(alarm, receive_count=1)

        assert claim.disposition is ClaimDisposition.CONTENDED
        assert not claim.acquired

    def test_claim_read_error_fails_closed(self, alarm):
        ddb = MagicMock()
        ddb.put_item.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ConditionalCheckFailedException",
                    "Message": "existing item",
                }
            },
            "PutItem",
        )
        ddb.get_item.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InternalServerError",
                    "Message": "read unavailable",
                }
            },
            "GetItem",
        )

        with (
            patch(
                "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
                "rca-sessions",
            ),
            pytest.raises(SessionOwnershipCheckError),
        ):
            DynamoDbSessionStore(ddb).claim_session(alarm, receive_count=2)

    def test_previous_claim_cannot_finalize_state_report_or_notification(self, claim_store, alarm):
        store, ddb, table_name = claim_store
        rca_id = build_rca_id(build_idempotency_key(alarm))
        first_token = _claimed_token(store, alarm, 1)
        assert store.update_state(rca_id, RcaSessionState.SCOPING, claim_token=first_token)
        second_token = _claimed_token(store, alarm, 2)

        with pytest.raises(SessionCancelledError):
            store.update_state(
                rca_id,
                RcaSessionState.SCOPING,
                claim_token=first_token,
            )
        with pytest.raises(SessionCancelledError):
            store.mark_failed(rca_id, error_reason="late failure", claim_token=first_token)

        _advance_to(store, rca_id, second_token, RcaSessionState.REPORT_GENERATION)
        notification = NotificationMessage(
            rca_id=rca_id,
            root_cause_summary="current result",
            severity="high",
        )
        with pytest.raises(SessionCancelledError):
            store.mark_completed(
                rca_id,
                report_s3_key="reports/strands/stale/report.md",
                claim_token=first_token,
            )
        current_key = f"reports/strands/{rca_id}/attempt-2-{second_token}/report.md"
        assert store.mark_completed(
            rca_id,
            completion_notification=notification,
            report_s3_key=current_key,
            claim_token=second_token,
        )
        assert store.mark_completion_notified(rca_id, claim_token=first_token) is False
        assert store.mark_completion_notified(rca_id, claim_token=second_token) is True

        item = _claimed_item(ddb, table_name, alarm)
        assert item["report_s3_key"]["S"] == current_key
        assert item["completion_notification_status"]["S"] == "SENT"


class TestCreateSession:
    def test_returns_none_when_no_table_name(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = create_session(alarm, dynamodb_client=dynamodb_client)
        assert result is None
        dynamodb_client.put_item.assert_not_called()

    def test_returns_none_when_no_client(self, alarm: AlarmPayload):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = create_session(alarm, dynamodb_client=None)
        assert result is None

    def test_creates_session_successfully(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            session = create_session(alarm, dynamodb_client=dynamodb_client)

        assert session is not None
        assert session.rca_id
        assert session.alarm_name == "HighCPU"
        assert session.state == RcaSessionState.ALARM_RECEIVED
        assert session.idempotency_key == "HighCPU#2025-06-01T12:00:00+00:00"
        assert session.engine == "strands"
        dynamodb_client.put_item.assert_called_once()

    def test_rca_id_is_deterministic(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            session = create_session(alarm, dynamodb_client=dynamodb_client)

        expected_rca_id = build_rca_id(build_idempotency_key(alarm))
        assert session.rca_id == expected_rca_id

    def test_put_item_uses_correct_table(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "my-table"):
            create_session(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.put_item.call_args[1]
        assert call_kwargs["TableName"] == "my-table"

    def test_put_item_uses_engine_prefixed_sk(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            create_session(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.put_item.call_args[1]
        assert call_kwargs["Item"]["SK"]["S"] == "strands#SESSION"

    def test_put_item_includes_engine_attribute(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            create_session(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.put_item.call_args[1]
        assert call_kwargs["Item"]["engine"]["S"] == "strands"
        assert call_kwargs["Item"]["alarm_name"]["S"] == "HighCPU"
        assert call_kwargs["Item"]["region"]["S"] == "us-east-1"

    def test_put_item_includes_condition_expression(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            create_session(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.put_item.call_args[1]
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(SK)"

    def test_returns_none_on_conditional_check_failed(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "dup"}}
        dynamodb_client.put_item.side_effect = ClientError(error_response, "PutItem")

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = create_session(alarm, dynamodb_client=dynamodb_client)

        assert result is None

    def test_raises_on_other_client_error(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.put_item.side_effect = ClientError(error_response, "PutItem")

        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"),
            pytest.raises(ClientError),
        ):
            create_session(alarm, dynamodb_client=dynamodb_client)


class TestCheckDuplicate:
    def test_returns_false_when_no_table_name(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = check_duplicate(alarm, dynamodb_client=dynamodb_client)
        assert result is False

    def test_returns_false_when_no_client(self, alarm: AlarmPayload):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = check_duplicate(alarm, dynamodb_client=None)
        assert result is False

    def test_returns_true_when_item_found(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        dynamodb_client.get_item.return_value = {"Item": {"PK": {"S": "RCA#abc"}, "SK": {"S": "strands#SESSION"}}}

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = check_duplicate(alarm, dynamodb_client=dynamodb_client)

        assert result is True

    def test_returns_false_when_no_item(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        dynamodb_client.get_item.return_value = {}

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = check_duplicate(alarm, dynamodb_client=dynamodb_client)

        assert result is False

    def test_returns_false_on_error(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.get_item.side_effect = ClientError(error_response, "GetItem")

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = check_duplicate(alarm, dynamodb_client=dynamodb_client)

        assert result is False

    def test_uses_get_item_with_correct_key(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        dynamodb_client.get_item.return_value = {}

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            check_duplicate(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.get_item.call_args[1]
        expected_rca_id = build_rca_id(build_idempotency_key(alarm))
        assert call_kwargs["Key"]["PK"]["S"] == f"RCA#{expected_rca_id}"
        assert call_kwargs["Key"]["SK"]["S"] == "strands#SESSION"


def _ddb_with_state(state: str) -> MagicMock:
    """Create a MagicMock DDB client that returns the given state for get_item."""
    ddb = MagicMock()
    ddb.get_item.return_value = {
        "Item": {
            "state": {"S": state},
            "claim_token": {"S": CLAIM_TOKEN},
        }
    }
    return ddb


class TestUpdateState:
    def test_returns_false_when_no_table(self, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = update_state("rca-123", RcaSessionState.SCOPING, dynamodb_client=dynamodb_client)
        assert result is False

    def test_returns_false_when_no_client(self):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = update_state("rca-123", RcaSessionState.SCOPING, dynamodb_client=None)
        assert result is False

    def test_updates_state_successfully(self):
        ddb = _ddb_with_state("ALARM_RECEIVED")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = update_state(
                "rca-123",
                RcaSessionState.SCOPING,
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )

        assert result is True
        ddb.update_item.assert_called_once()
        call_kwargs = ddb.update_item.call_args[1]
        assert call_kwargs["Key"]["PK"]["S"] == "RCA#rca-123"
        assert call_kwargs["Key"]["SK"]["S"] == "strands#SESSION"
        assert call_kwargs["ExpressionAttributeValues"][":state"]["S"] == "SCOPING"

    def test_includes_claim_and_expected_source_state_condition(self):
        ddb = _ddb_with_state("ALARM_RECEIVED")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            update_state(
                "rca-123",
                RcaSessionState.SCOPING,
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )

        call_kwargs = ddb.update_item.call_args[1]
        assert (
            call_kwargs["ConditionExpression"]
            == "attribute_exists(SK) AND claim_token = :claim AND #st = :expected_state"
        )
        assert call_kwargs["ExpressionAttributeValues"][":claim"]["S"] == CLAIM_TOKEN
        assert call_kwargs["ExpressionAttributeValues"][":expected_state"]["S"] == "ALARM_RECEIVED"

    def test_raises_session_cancelled_error(self):
        ddb = _ddb_with_state("ALARM_RECEIVED")
        error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "cancelled"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"),
            pytest.raises(SessionCancelledError),
        ):
            update_state(
                "rca-123",
                RcaSessionState.SCOPING,
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )

    def test_storage_error_fails_closed(self):
        ddb = _ddb_with_state("ALARM_RECEIVED")
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"),
            pytest.raises(SessionOwnershipCheckError),
        ):
            update_state(
                "rca-123",
                RcaSessionState.SCOPING,
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )


class TestExpectedStateFencing:
    @pytest.mark.parametrize(
        ("source_state", "write_kind", "target_state"),
        [
            ("ALARM_RECEIVED", "update", RcaSessionState.SCOPING),
            ("SCOPING", "failed", RcaSessionState.FAILED),
            ("SCOPING", "outdated", RcaSessionState.OUTDATED),
            ("REPORT_GENERATION", "completed", RcaSessionState.COMPLETED),
        ],
    )
    def test_every_session_state_write_checks_claim_and_exact_source(
        self,
        source_state,
        write_kind,
        target_state,
    ):
        ddb = _ddb_with_state(source_state)
        store = DynamoDbSessionStore(ddb)

        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            if write_kind == "update":
                result = store.update_state(
                    "rca-123",
                    target_state,
                    claim_token=CLAIM_TOKEN,
                )
            elif write_kind == "failed":
                result = store.mark_failed(
                    "rca-123",
                    claim_token=CLAIM_TOKEN,
                )
            elif write_kind == "outdated":
                result = store.mark_outdated(
                    "rca-123",
                    claim_token=CLAIM_TOKEN,
                )
            else:
                result = store.mark_completed(
                    "rca-123",
                    claim_token=CLAIM_TOKEN,
                )

        assert result is True
        call_kwargs = ddb.update_item.call_args.kwargs
        assert (
            call_kwargs["ConditionExpression"]
            == "attribute_exists(SK) AND claim_token = :claim AND #st = :expected_state"
        )
        values = call_kwargs["ExpressionAttributeValues"]
        assert values[":claim"]["S"] == CLAIM_TOKEN
        assert values[":expected_state"]["S"] == source_state
        assert values[":state"]["S"] == target_state.value

    @pytest.mark.parametrize(
        ("initial_state", "racing_state", "stale_write"),
        [
            (RcaSessionState.ALARM_RECEIVED, RcaSessionState.SCOPING, "failed"),
            (RcaSessionState.ALARM_RECEIVED, RcaSessionState.FAILED, "scoping"),
            (RcaSessionState.REPORT_GENERATION, RcaSessionState.COMPLETED, "failed"),
        ],
    )
    def test_same_claim_interleaving_blocks_stale_state_write(
        self,
        claim_store,
        alarm,
        monkeypatch,
        initial_state,
        racing_state,
        stale_write,
    ):
        store, ddb, table_name = claim_store
        rca_id = build_rca_id(build_idempotency_key(alarm))
        claim_token = _claimed_token(store, alarm, 1)
        if initial_state is RcaSessionState.REPORT_GENERATION:
            _advance_to(
                store,
                rca_id,
                claim_token,
                RcaSessionState.REPORT_GENERATION,
            )

        original_update_item = ddb.update_item
        race_injected = False

        def racing_update_item(**kwargs):
            nonlocal race_injected
            if not race_injected:
                race_injected = True
                original_update_item(
                    TableName=table_name,
                    Key={
                        "PK": {"S": f"RCA#{rca_id}"},
                        "SK": {"S": "strands#SESSION"},
                    },
                    UpdateExpression="SET #st = :racing_state",
                    ExpressionAttributeNames={"#st": "state"},
                    ExpressionAttributeValues={
                        ":racing_state": {"S": racing_state.value},
                    },
                )
            return original_update_item(**kwargs)

        monkeypatch.setattr(ddb, "update_item", racing_update_item)

        with pytest.raises(SessionCancelledError):
            if stale_write == "scoping":
                store.update_state(
                    rca_id,
                    RcaSessionState.SCOPING,
                    claim_token=claim_token,
                )
            else:
                store.mark_failed(
                    rca_id,
                    error_reason="stale failure",
                    claim_token=claim_token,
                )

        assert race_injected
        item = _claimed_item(ddb, table_name, alarm)
        assert item["state"]["S"] == racing_state.value
        assert item["claim_token"]["S"] == claim_token

    @pytest.mark.parametrize(
        ("initial_state", "stale_write"),
        [
            (RcaSessionState.ALARM_RECEIVED, "scoping"),
            (RcaSessionState.SCOPING, "failed"),
            (RcaSessionState.REPORT_GENERATION, "completed"),
        ],
    )
    def test_reclaim_after_validation_blocks_every_previous_claim_write(
        self,
        claim_store,
        alarm,
        monkeypatch,
        initial_state,
        stale_write,
    ):
        store, ddb, table_name = claim_store
        rca_id = build_rca_id(build_idempotency_key(alarm))
        previous_claim = _claimed_token(store, alarm, 1)
        if initial_state is not RcaSessionState.ALARM_RECEIVED:
            _advance_to(store, rca_id, previous_claim, initial_state)

        original_update_item = ddb.update_item
        reclaimed = None

        def reclaim_before_stale_update(**kwargs):
            nonlocal reclaimed
            if reclaimed is None:
                reclaimed = store.claim_session(
                    alarm,
                    receive_count=2,
                    message_id=MESSAGE_ID,
                )
                assert reclaimed.acquired
            return original_update_item(**kwargs)

        monkeypatch.setattr(ddb, "update_item", reclaim_before_stale_update)

        with pytest.raises(SessionCancelledError):
            if stale_write == "scoping":
                store.update_state(
                    rca_id,
                    RcaSessionState.SCOPING,
                    claim_token=previous_claim,
                )
            elif stale_write == "failed":
                store.mark_failed(
                    rca_id,
                    error_reason="stale failure",
                    claim_token=previous_claim,
                )
            else:
                store.mark_completed(
                    rca_id,
                    root_cause="stale result",
                    report_s3_key="reports/stale/report.md",
                    claim_token=previous_claim,
                )

        assert reclaimed is not None
        assert reclaimed.claim_token
        assert reclaimed.claim_token != previous_claim
        item = _claimed_item(ddb, table_name, alarm)
        assert item["state"]["S"] == RcaSessionState.ALARM_RECEIVED.value
        assert item["claim_token"]["S"] == reclaimed.claim_token
        assert item["receive_count"]["N"] == "2"
        assert item["message_id"]["S"] == MESSAGE_ID
        assert "error_reason" not in item
        assert "root_cause" not in item
        assert "report_s3_key" not in item


class TestMarkCompleted:
    def test_returns_false_when_no_table(self, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = mark_completed("rca-123", dynamodb_client=dynamodb_client)
        assert result is False

    def test_marks_completed_with_root_cause(self):
        ddb = _ddb_with_state("REPORT_GENERATION")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_completed(
                "rca-123",
                root_cause="Bad deploy",
                confirmed=True,
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )

        assert result is True
        call_kwargs = ddb.update_item.call_args[1]
        assert call_kwargs["Key"]["SK"]["S"] == "strands#SESSION"
        assert call_kwargs["ExpressionAttributeValues"][":state"]["S"] == "COMPLETED"
        assert call_kwargs["ExpressionAttributeValues"][":rc"]["S"] == "Bad deploy"
        assert call_kwargs["ExpressionAttributeValues"][":cf"]["BOOL"] is True

    def test_completion_persists_selected_hypothesis_and_pending_notification(self):
        ddb = _ddb_with_state("REPORT_GENERATION")
        notification = NotificationMessage(
            rca_id="rca-123",
            root_cause_summary="database connection leak",
            severity="high",
            selected_hypothesis_id="h-selected",
            fault_type=FaultType.DB_CONNECTION_LEAK,
        )
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_completed(
                "rca-123",
                root_cause="database connection leak",
                confirmed=True,
                selected_hypothesis_id="h-selected",
                fault_type=FaultType.DB_CONNECTION_LEAK,
                completion_notification=notification,
                report_s3_key="reports/strands/rca-123/attempt-1/report.md",
                playbook_span_id="span-playbook-1",
                playbook_id="playbook-1",
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )

        assert result is True
        values = ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":hid"]["S"] == "h-selected"
        assert values[":fault_type"]["S"] == FaultType.DB_CONNECTION_LEAK.value
        assert values[":notification_pending"]["S"] == "PENDING"
        assert NotificationMessage.model_validate_json(values[":notification"]["S"]) == notification
        assert values[":report_s3_key"]["S"] == "reports/strands/rca-123/attempt-1/report.md"
        assert values[":playbook_span_id"]["S"] == "span-playbook-1"
        assert values[":playbook_id"]["S"] == "playbook-1"

    def test_storage_error_fails_closed(self):
        ddb = _ddb_with_state("REPORT_GENERATION")
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"),
            pytest.raises(SessionOwnershipCheckError),
        ):
            mark_completed(
                "rca-123",
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )


class TestMarkFailed:
    def test_returns_false_when_no_table(self, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = mark_failed("rca-123", dynamodb_client=dynamodb_client)
        assert result is False

    def test_marks_failed_with_reason(self):
        ddb = _ddb_with_state("SCOPING")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_failed(
                "rca-123",
                error_reason="Pipeline crash",
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )

        assert result is True
        call_kwargs = ddb.update_item.call_args[1]
        assert call_kwargs["Key"]["SK"]["S"] == "strands#SESSION"
        assert call_kwargs["ExpressionAttributeValues"][":state"]["S"] == "FAILED"
        assert call_kwargs["ExpressionAttributeValues"][":err"]["S"] == "Pipeline crash"

    def test_storage_error_fails_closed(self):
        ddb = _ddb_with_state("SCOPING")
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"),
            pytest.raises(SessionOwnershipCheckError),
        ):
            mark_failed(
                "rca-123",
                claim_token=CLAIM_TOKEN,
                dynamodb_client=ddb,
            )
