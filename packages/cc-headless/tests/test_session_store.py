import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from cc_headless.adapters.secondary.session import dynamodb_session_store
from cc_headless.adapters.secondary.session.dynamodb_session_store import (
    DynamoDbSessionStore,
    SessionCancelledError,
    build_active_incident_pk,
    build_alarm_identity,
    build_idempotency_key,
    build_rca_id,
)
from cc_headless.ports.interfaces.session_store import (
    ClaimDisposition,
    IncidentAlarm,
    IncidentClaimDisposition,
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


def _incident_alarm(
    moment: datetime,
    *,
    state: str = "ALARM",
    alarm_arn: str | None = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:HighCPU",
) -> IncidentAlarm:
    return IncidentAlarm(
        alarm_name="HighCPU",
        alarm_arn=alarm_arn,
        region="us-east-1",
        state_change_time=moment,
        new_state=state,
    )


def _incident_item(ddb, table_name: str, alarm: IncidentAlarm) -> dict:
    return ddb.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": build_active_incident_pk(alarm)},
            "SK": {"S": "ACTIVE_INCIDENT"},
        },
        ConsistentRead=True,
    )["Item"]


class TestActiveIncident:
    def test_uses_alarm_arn_or_stable_name_fallback_for_identity(self):
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))

        assert build_alarm_identity(alarm) == alarm.alarm_arn
        without_arn = _incident_alarm(alarm.state_change_time, alarm_arn=None)
        assert build_alarm_identity(without_arn) == "cloudwatch:us-east-1:alarm:HighCPU"
        assert build_active_incident_pk(alarm).startswith("ALARM#")
        assert len(build_active_incident_pk(alarm)) == len("ALARM#") + 64

    def test_same_event_allows_both_engine_sessions(self, session_store):
        store, ddb, table_name = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))

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
                "SK": {"S": "strands#SESSION"},
                "state": {"S": "ALARM_RECEIVED"},
            },
        )
        claim = store.claim_session(
            first.candidate_rca_id,
            alarm.alarm_name,
            build_idempotency_key(alarm),
            receive_count=1,
        )
        assert claim.acquired

    def test_realarm_is_suppressed_while_analysis_is_active(self, session_store):
        store, ddb, table_name = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        ddb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"RCA#{opened.candidate_rca_id}"},
                "SK": {"S": "cc-headless#SESSION"},
                "state": {"S": "ANALYZING"},
            },
        )
        realarm = _incident_alarm(alarm.state_change_time + timedelta(minutes=18))

        claim = store.claim_incident(realarm, cooldown_seconds=300)

        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.candidate_rca_id == opened.candidate_rca_id
        assert "cc-headless#SESSION" in claim.reason
        assert claim.retryable

    def test_realarm_is_suppressed_while_execution_is_active(self, session_store):
        store, ddb, table_name = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        ddb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"RCA#{opened.candidate_rca_id}"},
                "SK": {"S": "EXEC_ACTIVE"},
            },
        )

        claim = store.claim_incident(
            _incident_alarm(alarm.state_change_time + timedelta(minutes=18)),
            cooldown_seconds=300,
        )

        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.reason == "EXEC_ACTIVE exists"
        assert claim.retryable

    def test_ok_immediate_realarm_is_suppressed_during_cooldown(self, session_store):
        store, _, _ = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        assert store.record_recovery(_incident_alarm(ok_at, state="OK"))

        claim = store.claim_incident(
            _incident_alarm(ok_at + timedelta(seconds=299)),
            cooldown_seconds=300,
        )

        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.candidate_rca_id == opened.candidate_rca_id
        assert claim.reason == "recovery cooldown has not elapsed"
        assert not claim.retryable

    def test_ok_before_alarm_persists_recovery_watermark_and_suppresses_closed_event(self, session_store):
        store, ddb, table_name = session_store
        ok_at = datetime(2026, 8, 7, 12, tzinfo=UTC)
        recovery = _incident_alarm(ok_at, state="OK")

        assert store.record_recovery(recovery)

        watermark = _incident_item(ddb, table_name, recovery)
        assert watermark["last_ok_at"]["S"] == ok_at.isoformat()
        assert "candidate_rca_id" not in watermark
        assert "generation" not in watermark

        delayed_alarm = store.claim_incident(
            _incident_alarm(ok_at - timedelta(minutes=1)),
            cooldown_seconds=300,
        )
        cooldown_alarm = store.claim_incident(
            _incident_alarm(ok_at + timedelta(seconds=299)),
            cooldown_seconds=300,
        )

        assert delayed_alarm.disposition is IncidentClaimDisposition.SUPPRESSED
        assert cooldown_alarm.disposition is IncidentClaimDisposition.SUPPRESSED
        assert not delayed_alarm.retryable
        assert not cooldown_alarm.retryable
        assert "candidate_rca_id" not in _incident_item(ddb, table_name, recovery)

    def test_ok_before_alarm_opens_generation_after_event_time_cooldown(self, session_store):
        store, _, _ = session_store
        ok_at = datetime(2026, 8, 7, 12, tzinfo=UTC)
        recovery = _incident_alarm(ok_at, state="OK")
        alarm = _incident_alarm(ok_at + timedelta(seconds=300))

        assert store.record_recovery(recovery)
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        duplicate = store.claim_incident(alarm, cooldown_seconds=300)

        assert opened.disposition is IncidentClaimDisposition.PROCEED
        assert opened.generation == 1
        assert duplicate.disposition is IncidentClaimDisposition.PROCEED
        assert duplicate.candidate_rca_id == opened.candidate_rca_id

    def test_older_alarm_than_active_generation_is_permanently_suppressed(self, session_store):
        store, _, _ = session_store
        opened_at = datetime(2026, 8, 7, 12, tzinfo=UTC)
        opened = store.claim_incident(_incident_alarm(opened_at), cooldown_seconds=300)

        claim = store.claim_incident(
            _incident_alarm(opened_at - timedelta(seconds=1)),
            cooldown_seconds=300,
        )

        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.candidate_rca_id == opened.candidate_rca_id
        assert not claim.retryable
        assert claim.reason == "alarm event-time does not follow the active incident"

    def test_terminal_incident_without_ok_retries_realarm(self, session_store):
        store, _, _ = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        opened = store.claim_incident(alarm, cooldown_seconds=300)

        claim = store.claim_incident(
            _incident_alarm(alarm.state_change_time + timedelta(minutes=18)),
            cooldown_seconds=300,
        )

        assert claim.disposition is IncidentClaimDisposition.SUPPRESSED
        assert claim.candidate_rca_id == opened.candidate_rca_id
        assert claim.reason == "incident has no recovery observation"
        assert claim.retryable

    def test_out_of_order_realarm_proceeds_after_delayed_ok_arrives(self, session_store):
        store, ddb, table_name = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        ddb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"RCA#{opened.candidate_rca_id}"},
                "SK": {"S": "cc-headless#SESSION"},
                "state": {"S": "COMPLETED"},
            },
        )
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        realarm = _incident_alarm(ok_at + timedelta(seconds=301))

        pending = store.claim_incident(realarm, cooldown_seconds=300)
        assert pending.disposition is IncidentClaimDisposition.SUPPRESSED
        assert pending.retryable

        assert store.record_recovery(_incident_alarm(ok_at, state="OK"))
        claim = store.claim_incident(realarm, cooldown_seconds=300)

        assert claim.disposition is IncidentClaimDisposition.PROCEED
        assert claim.generation == 2
        assert claim.candidate_rca_id != opened.candidate_rca_id
        assert store.claim_incident(realarm, cooldown_seconds=300).candidate_rca_id == claim.candidate_rca_id

    def test_older_recovery_does_not_replace_latest_ok(self, session_store):
        store, ddb, table_name = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        store.claim_incident(alarm, cooldown_seconds=300)
        latest_ok = alarm.state_change_time + timedelta(minutes=10)
        older_ok = latest_ok - timedelta(minutes=1)

        assert store.record_recovery(_incident_alarm(latest_ok, state="OK"))
        assert store.record_recovery(_incident_alarm(older_ok, state="OK"))

        item = _incident_item(ddb, table_name, alarm)
        assert item["last_ok_at"]["S"] == latest_ok.isoformat()

    def test_terminal_sessions_after_cooldown_allow_new_generation(self, session_store):
        store, ddb, table_name = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        for sk in ("strands#SESSION", "cc-headless#SESSION"):
            ddb.put_item(
                TableName=table_name,
                Item={
                    "PK": {"S": f"RCA#{opened.candidate_rca_id}"},
                    "SK": {"S": sk},
                    "state": {"S": "COMPLETED"},
                },
            )
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        assert store.record_recovery(_incident_alarm(ok_at, state="OK"))

        claim = store.claim_incident(
            _incident_alarm(ok_at + timedelta(seconds=301)),
            cooldown_seconds=300,
        )

        assert claim.disposition is IncidentClaimDisposition.PROCEED
        assert claim.generation == 2
        assert claim.candidate_rca_id != opened.candidate_rca_id
        item = _incident_item(ddb, table_name, alarm)
        assert item["candidate_rca_id"]["S"] == claim.candidate_rca_id
        assert item["generation"]["N"] == "2"
        assert "last_ok_at" not in item

    def test_generation_transaction_contention_preserves_winner(self, session_store, monkeypatch):
        store, ddb, table_name = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        opened = store.claim_incident(alarm, cooldown_seconds=300)
        ok_at = alarm.state_change_time + timedelta(minutes=10)
        assert store.record_recovery(_incident_alarm(ok_at, state="OK"))
        contender = _incident_alarm(ok_at + timedelta(seconds=301))
        winner = _incident_alarm(ok_at + timedelta(seconds=302))
        winner_rca_id = build_rca_id(build_idempotency_key(winner))
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
                        ":opened": {"S": winner.state_change_time.isoformat()},
                        ":alarm": {"S": winner.state_change_time.isoformat()},
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

    def test_incident_gate_preserves_session_redelivery_semantics(self, session_store):
        store, _, _ = session_store
        alarm = _incident_alarm(datetime(2026, 8, 7, 12, tzinfo=UTC))
        assert store.claim_incident(alarm, cooldown_seconds=300).acquired
        first = _claim(store, 1)
        assert first.acquired

        assert store.claim_incident(alarm, cooldown_seconds=300).acquired
        assert _claim(store, 1).disposition is ClaimDisposition.CONTENDED
        assert _claim(store, 2).acquired


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
