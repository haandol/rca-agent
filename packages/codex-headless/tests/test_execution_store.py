import json

import boto3
import pytest
from moto import mock_aws

from codex_headless.adapters.secondary.execution import dynamodb_execution_store
from codex_headless.adapters.secondary.execution.dynamodb_execution_store import (
    DynamoDbExecutionStore,
)
from codex_headless.ports.interfaces.execution_store import (
    ExecutionClaimDisposition,
    ExecutionClaimLostError,
    ExecutionTargetUnavailableError,
)
from codex_headless.services.execution_state import (
    ExecutionState,
    InvalidExecutionTransitionError,
)

TABLE = "rca-sessions"
RCA_ID = "rca-1"
ENGINE = "codex-headless"
EXECUTION_ID = "exec-1"
REPORT_KEY = "reports/codex-headless/rca-1/report.md"
SNAPSHOT_KEY = "approved/rca-1/exec-1/playbook.json"
DIGEST = "a" * 64
PLAYBOOK_STEPS = [
    {
        "step_id": "step-1",
        "intent": "커넥션 회수",
        "action": "api 서비스를 강제 재배포",
        "success_criteria": "DatabaseConnections 20 이하",
    }
]
PLAYBOOK = {
    "playbook_id": "pb-1",
    "failure_type": "DB 커넥션 누수",
    "execution_steps": PLAYBOOK_STEPS,
}


@pytest.fixture
def store(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
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
        monkeypatch.setattr(dynamodb_execution_store, "DYNAMODB_TABLE_NAME", TABLE)
        yield DynamoDbExecutionStore(ddb), ddb


def _put_completed_session(ddb, *, state: str = "COMPLETED") -> None:
    ddb.put_item(
        TableName=TABLE,
        Item={
            "PK": {"S": f"RCA#{RCA_ID}"},
            "SK": {"S": f"{ENGINE}#SESSION"},
            "state": {"S": state},
            "alarm_name": {"S": "VitalIngestFailure"},
            "report_s3_key": {"S": REPORT_KEY},
            "alarm_data": {"S": json.dumps({"AlarmName": "VitalIngestFailure", "Trigger": {"MetricName": "Vital"}})},
        },
    )


def _put_reservation(ddb, *, approval_id: str = "approval-1", digest: str = DIGEST) -> None:
    ddb.put_item(
        TableName=TABLE,
        Item={
            "PK": {"S": f"RCA#{RCA_ID}"},
            "SK": {"S": f"EXEC#{EXECUTION_ID}"},
            "execution_id": {"S": EXECUTION_ID},
            "rca_id": {"S": RCA_ID},
            "engine": {"S": ENGINE},
            "approval_id": {"S": approval_id},
            "requested_by": {"S": "operator"},
            "report_s3_key": {"S": REPORT_KEY},
            "approved_playbook_s3_key": {"S": SNAPSHOT_KEY},
            "playbook_digest": {"S": digest},
            "execution_state": {"S": "PENDING_APPROVAL"},
        },
    )
    ddb.put_item(
        TableName=TABLE,
        Item={
            "PK": {"S": f"RCA#{RCA_ID}"},
            "SK": {"S": "EXEC_ACTIVE"},
            "execution_id": {"S": EXECUTION_ID},
        },
    )


def _claim(store: DynamoDbExecutionStore, *, approval_id: str = "approval-1"):
    if not store._get_execution(RCA_ID, EXECUTION_ID):
        _put_reservation(store._ddb, approval_id=approval_id)
    return store.claim_execution(
        EXECUTION_ID,
        rca_id=RCA_ID,
        engine=ENGINE,
        approval_id=approval_id,
        requested_by="operator",
        report_s3_key=REPORT_KEY,
        approved_playbook_s3_key=SNAPSHOT_KEY,
        playbook_digest=DIGEST,
        claim_seconds=600,
    )


def _execution_item(ddb) -> dict:
    return ddb.get_item(
        TableName=TABLE,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": f"EXEC#{EXECUTION_ID}"}},
        ConsistentRead=True,
    )["Item"]


def test_the_first_request_claims_the_execution(store):
    execution_store, _ = store

    claim = _claim(execution_store)

    assert claim.acquired
    assert claim.attempt == 1


def test_a_redelivered_request_does_not_start_a_second_execution(store):
    """같은 승인의 재전달이 실행을 중복시키면 승인 한 번이 두 번의 쓰기가 된다."""
    execution_store, _ = store
    first = _claim(execution_store)

    second = _claim(execution_store)

    assert first.acquired
    assert second.disposition is ExecutionClaimDisposition.CONTENDED


def test_a_redelivered_request_after_a_terminal_execution_is_acknowledged(store):
    execution_store, ddb = store
    claim = _claim(execution_store)
    _put_completed_session(ddb)
    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.VERIFYING,
        claim_token=claim.claim_token,
    )
    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.RESOLVED,
        claim_token=claim.claim_token,
    )

    redelivered = _claim(execution_store)

    assert redelivered.disposition is ExecutionClaimDisposition.TERMINAL_DUPLICATE


def test_an_expired_claim_is_failed_and_never_reclaimed(store):
    execution_store, ddb = store
    _claim(execution_store)
    ddb.update_item(
        TableName=TABLE,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": f"EXEC#{EXECUTION_ID}"}},
        UpdateExpression="SET claim_expires_at = :expired",
        ExpressionAttributeValues={":expired": {"N": "1"}},
    )

    disposition = _claim(execution_store)

    assert disposition.disposition is ExecutionClaimDisposition.EXPIRED_FAILED
    item = _execution_item(ddb)
    assert item["execution_state"]["S"] == "FAILED"
    assert "reapproval" in item["error_reason"]["S"]
    assert "Item" not in ddb.get_item(
        TableName=TABLE,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": "EXEC_ACTIVE"}},
    )


def test_a_terminal_execution_releases_its_claim(store):
    execution_store, ddb = store
    claim = _claim(execution_store)
    _put_completed_session(ddb)
    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.FAILED,
        claim_token=claim.claim_token,
        error_reason="target unavailable",
    )

    item = _execution_item(ddb)

    assert "claim_expires_at" not in item
    assert item["error_reason"]["S"] == "target unavailable"
    assert "Item" not in ddb.get_item(
        TableName=TABLE,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": "EXEC_ACTIVE"}},
    )


def test_a_terminal_transition_cannot_release_another_executions_active_marker(store):
    execution_store, ddb = store
    claim = _claim(execution_store)
    ddb.put_item(
        TableName=TABLE,
        Item={
            "PK": {"S": f"RCA#{RCA_ID}"},
            "SK": {"S": "EXEC_ACTIVE"},
            "execution_id": {"S": "exec-other"},
        },
    )

    with pytest.raises(ExecutionClaimLostError):
        execution_store.update_state(
            EXECUTION_ID,
            rca_id=RCA_ID,
            state=ExecutionState.FAILED,
            claim_token=claim.claim_token,
            error_reason="target unavailable",
        )

    assert _execution_item(ddb)["execution_state"]["S"] == "EXECUTING"
    active = ddb.get_item(
        TableName=TABLE,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": "EXEC_ACTIVE"}},
    )["Item"]
    assert active["execution_id"]["S"] == "exec-other"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("approval_id", "other-approval"),
        ("report_s3_key", "reports/other.md"),
        ("approved_playbook_s3_key", "approved/other.json"),
        ("playbook_digest", "b" * 64),
    ],
)
def test_a_mismatched_reservation_is_rejected_without_mutation(store, field, value):
    execution_store, ddb = store
    _put_reservation(ddb)
    kwargs = {
        "rca_id": RCA_ID,
        "engine": ENGINE,
        "approval_id": "approval-1",
        "requested_by": "operator",
        "report_s3_key": REPORT_KEY,
        "approved_playbook_s3_key": SNAPSHOT_KEY,
        "playbook_digest": DIGEST,
        "claim_seconds": 600,
    }
    kwargs[field] = value

    claim = execution_store.claim_execution(EXECUTION_ID, **kwargs)

    assert claim.disposition is ExecutionClaimDisposition.REJECTED
    assert _execution_item(ddb)["execution_state"]["S"] == "PENDING_APPROVAL"


def test_a_missing_reservation_is_rejected_and_never_created(store):
    execution_store, ddb = store

    claim = execution_store.claim_execution(
        EXECUTION_ID,
        rca_id=RCA_ID,
        engine=ENGINE,
        approval_id="approval-1",
        requested_by="operator",
        report_s3_key=REPORT_KEY,
        approved_playbook_s3_key=SNAPSHOT_KEY,
        playbook_digest=DIGEST,
        claim_seconds=600,
    )

    assert claim.disposition is ExecutionClaimDisposition.REJECTED
    assert "Item" not in ddb.get_item(
        TableName=TABLE,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": f"EXEC#{EXECUTION_ID}"}},
    )


def test_a_reservation_without_its_active_marker_is_rejected(store):
    execution_store, ddb = store
    _put_reservation(ddb)
    ddb.delete_item(
        TableName=TABLE,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": "EXEC_ACTIVE"}},
    )

    claim = _claim(execution_store)

    assert claim.disposition is ExecutionClaimDisposition.REJECTED
    assert _execution_item(ddb)["execution_state"]["S"] == "PENDING_APPROVAL"


def test_a_lost_claim_cannot_write_the_state(store):
    execution_store, _ = store
    _claim(execution_store)

    with pytest.raises(ExecutionClaimLostError):
        execution_store.update_state(
            EXECUTION_ID,
            rca_id=RCA_ID,
            state=ExecutionState.VERIFYING,
            claim_token="some-other-worker",
        )


def test_the_store_refuses_to_skip_the_verification_state(store):
    execution_store, _ = store
    claim = _claim(execution_store)

    with pytest.raises(InvalidExecutionTransitionError):
        execution_store.update_state(
            EXECUTION_ID,
            rca_id=RCA_ID,
            state=ExecutionState.RESOLVED,
            claim_token=claim.claim_token,
        )


def test_the_target_combines_the_approved_snapshot_with_completed_analysis_context(store):
    execution_store, ddb = store
    _put_completed_session(ddb)

    target = execution_store.load_target(
        RCA_ID,
        ENGINE,
        report_s3_key=REPORT_KEY,
        playbook=PLAYBOOK,
    )

    assert target.playbook["playbook_id"] == "pb-1"
    assert target.playbook["execution_steps"] == PLAYBOOK_STEPS
    assert target.alarm_name == "VitalIngestFailure"
    assert target.metric_name == "Vital"


@pytest.mark.parametrize("state", ["ANALYZING", "FAILED", "CANCELLED", "OUTDATED"])
def test_an_unfinished_analysis_has_no_approvable_playbook(store, state):
    execution_store, ddb = store
    _put_completed_session(ddb, state=state)

    with pytest.raises(ExecutionTargetUnavailableError):
        execution_store.load_target(RCA_ID, ENGINE, report_s3_key=REPORT_KEY, playbook=PLAYBOOK)


def test_a_report_key_mismatch_is_rejected(store):
    execution_store, ddb = store
    _put_completed_session(ddb)

    with pytest.raises(ExecutionTargetUnavailableError):
        execution_store.load_target(RCA_ID, ENGINE, report_s3_key="reports/other.md", playbook=PLAYBOOK)


def test_a_revision_cannot_change_an_already_approved_snapshot(store):
    execution_store, ddb = store
    _put_completed_session(ddb)
    revised = {
        "playbook_id": "pb-1",
        "failure_type": "DB 커넥션 누수",
        "execution_steps": [
            {**PLAYBOOK_STEPS[0], "action": "api 서비스를 강제 재배포하고 30초 대기"},
        ],
    }
    execution_store.save_playbook_revision(RCA_ID, ENGINE, revised, execution_id=EXECUTION_ID)

    target = execution_store.load_target(
        RCA_ID,
        ENGINE,
        report_s3_key=REPORT_KEY,
        playbook=PLAYBOOK,
    )

    assert target.playbook["execution_steps"][0]["action"] == "api 서비스를 강제 재배포"


def test_revision_is_staged_before_atomic_publication(store):
    execution_store, ddb = store
    revised = {**PLAYBOOK, "verification_status": "VERIFIED"}

    execution_store.save_playbook_revision(RCA_ID, ENGINE, revised, execution_id=EXECUTION_ID)

    stage_key = {
        "PK": {"S": f"RCA#{RCA_ID}"},
        "SK": {"S": f"{ENGINE}#PLAYBOOK_REVISION_STAGE#{EXECUTION_ID}"},
    }
    canonical_key = {
        "PK": {"S": f"RCA#{RCA_ID}"},
        "SK": {"S": f"{ENGINE}#PLAYBOOK_REVISION"},
    }
    staged = ddb.get_item(TableName=TABLE, Key=stage_key, ConsistentRead=True)["Item"]
    assert staged["publication_status"]["S"] == "PENDING"
    assert "Item" not in ddb.get_item(TableName=TABLE, Key=canonical_key, ConsistentRead=True)

    execution_store.publish_playbook_revision(RCA_ID, ENGINE, revised, execution_id=EXECUTION_ID)

    published = ddb.get_item(TableName=TABLE, Key=canonical_key, ConsistentRead=True)["Item"]
    assert published["publication_status"]["S"] == "PUBLISHED"
    assert published["revised_by_execution_id"]["S"] == EXECUTION_ID
    assert "Item" not in ddb.get_item(TableName=TABLE, Key=stage_key, ConsistentRead=True)


def test_only_one_retrospective_runs_per_execution(store):
    execution_store, ddb = store
    claim = _claim(execution_store)
    _put_completed_session(ddb)
    execution_store.update_state(
        EXECUTION_ID, rca_id=RCA_ID, state=ExecutionState.VERIFYING, claim_token=claim.claim_token
    )
    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.RESOLVED,
        claim_token=claim.claim_token,
        evidence_s3_key="executions/rca-1/exec-1/evidence.json",
    )

    first = execution_store.claim_retrospective(EXECUTION_ID, rca_id=RCA_ID, claim_token=claim.claim_token)
    second = execution_store.claim_retrospective(EXECUTION_ID, rca_id=RCA_ID, claim_token=claim.claim_token)

    assert first
    assert not second


def test_a_resolved_execution_without_evidence_records_failed_retrospective_and_cannot_claim(store):
    execution_store, ddb = store
    claim = _claim(execution_store)
    _put_completed_session(ddb)
    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.VERIFYING,
        claim_token=claim.claim_token,
    )
    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.RESOLVED,
        claim_token=claim.claim_token,
        retrospective_failure_reason="durable execution evidence is unavailable",
    )

    item = _execution_item(ddb)

    assert item["execution_state"]["S"] == "RESOLVED"
    assert "evidence_s3_key" not in item
    assert item["retrospective_status"]["S"] == "FAILED"
    assert "durable execution evidence" in item["retrospective_summary"]["S"]
    assert not execution_store.claim_retrospective(
        EXECUTION_ID,
        rca_id=RCA_ID,
        claim_token=claim.claim_token,
    )


@pytest.mark.parametrize("empty_evidence_attribute", [False, True])
def test_a_retrospective_claim_requires_a_non_empty_evidence_key(store, empty_evidence_attribute):
    execution_store, ddb = store
    claim = _claim(execution_store)
    _put_completed_session(ddb)
    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.VERIFYING,
        claim_token=claim.claim_token,
    )
    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.RESOLVED,
        claim_token=claim.claim_token,
    )
    if empty_evidence_attribute:
        ddb.update_item(
            TableName=TABLE,
            Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": f"EXEC#{EXECUTION_ID}"}},
            UpdateExpression="SET evidence_s3_key = :empty",
            ExpressionAttributeValues={":empty": {"S": ""}},
        )

    assert not execution_store.claim_retrospective(
        EXECUTION_ID,
        rca_id=RCA_ID,
        claim_token=claim.claim_token,
    )


@pytest.mark.parametrize("state", [ExecutionState.UNRESOLVED, ExecutionState.FAILED, ExecutionState.CANCELLED])
def test_an_unresolved_execution_cannot_claim_a_retrospective(store, state):
    """해소하지 못한 절차를 검증된 절차로 승격하면 플레이북이 나빠진다."""
    execution_store, ddb = store
    claim = _claim(execution_store)
    _put_completed_session(ddb)
    if state is ExecutionState.UNRESOLVED:
        execution_store.update_state(
            EXECUTION_ID, rca_id=RCA_ID, state=ExecutionState.VERIFYING, claim_token=claim.claim_token
        )
    execution_store.update_state(
        EXECUTION_ID, rca_id=RCA_ID, state=state, claim_token=claim.claim_token, error_reason="not resolved"
    )

    assert not execution_store.claim_retrospective(EXECUTION_ID, rca_id=RCA_ID, claim_token=claim.claim_token)


def test_the_execution_item_is_not_partitioned_by_engine(store):
    """실행 경로는 엔진과 무관하게 하나이므로 엔진으로 분리할 대상이 아니다."""
    execution_store, ddb = store
    _claim(execution_store)

    item = _execution_item(ddb)

    assert item["SK"]["S"] == f"EXEC#{EXECUTION_ID}"
    assert item["engine"]["S"] == ENGINE


def test_evidence_summary_and_key_land_on_the_execution_item(store):
    execution_store, ddb = store
    claim = _claim(execution_store)
    _put_completed_session(ddb)

    execution_store.update_state(
        EXECUTION_ID,
        rca_id=RCA_ID,
        state=ExecutionState.VERIFYING,
        claim_token=claim.claim_token,
        summary={"attempted_step_count": 2, "blocked_count": 1, "resolution_confirmed": None},
        evidence_s3_key="executions/rca-1/exec-1/evidence.json",
    )

    item = _execution_item(ddb)

    assert item["evidence_summary"]["M"]["attempted_step_count"]["N"] == "2"
    assert item["evidence_summary"]["M"]["blocked_count"]["N"] == "1"
    assert item["evidence_summary"]["M"]["resolution_confirmed"]["NULL"] is True
    assert item["evidence_s3_key"]["S"] == "executions/rca-1/exec-1/evidence.json"
