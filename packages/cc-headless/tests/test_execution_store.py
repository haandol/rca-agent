import json

import boto3
import pytest
from moto import mock_aws

from cc_headless.adapters.secondary.execution import dynamodb_execution_store
from cc_headless.adapters.secondary.execution.dynamodb_execution_store import (
    DynamoDbExecutionStore,
)
from cc_headless.ports.interfaces.execution_store import (
    ExecutionClaimDisposition,
    ExecutionClaimLostError,
    ExecutionTargetUnavailableError,
)
from cc_headless.services.execution_state import (
    ExecutionState,
    InvalidExecutionTransitionError,
)

TABLE = "rca-sessions"
RCA_ID = "rca-1"
ENGINE = "cc-headless"
EXECUTION_ID = "exec-1"
PLAYBOOK_STEPS = [
    {
        "step_id": "step-1",
        "intent": "커넥션 회수",
        "action": "api 서비스를 강제 재배포",
        "success_criteria": "DatabaseConnections 20 이하",
    }
]


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
            "report_s3_key": {"S": "reports/cc-headless/rca-1/report.md"},
            "alarm_data": {"S": json.dumps({"AlarmName": "VitalIngestFailure", "Trigger": {"MetricName": "Vital"}})},
        },
    )


def _put_playbook_span(ddb) -> None:
    ddb.put_item(
        TableName=TABLE,
        Item={
            "PK": {"S": f"RCA#{RCA_ID}"},
            "SK": {"S": f"{ENGINE}#SPAN#span-1"},
            "span_type": {"S": "PLAYBOOK"},
            "metadata": {
                "M": {
                    "playbook_id": {"S": "pb-1"},
                    "failure_type": {"S": "DB 커넥션 누수"},
                    "verification_status": {"S": "DRAFT"},
                    "tags": {"L": [{"S": "db-leak"}]},
                    "execution_steps": {"L": [{"M": {key: {"S": value} for key, value in PLAYBOOK_STEPS[0].items()}}]},
                }
            },
        },
    )


def _claim(store: DynamoDbExecutionStore, *, approval_id: str = "approval-1"):
    return store.claim_execution(
        EXECUTION_ID,
        rca_id=RCA_ID,
        engine=ENGINE,
        approval_id=approval_id,
        requested_by="operator",
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


def test_an_expired_claim_can_be_reclaimed_as_a_new_attempt(store):
    execution_store, ddb = store
    _claim(execution_store)
    ddb.update_item(
        TableName=TABLE,
        Key={"PK": {"S": f"RCA#{RCA_ID}"}, "SK": {"S": f"EXEC#{EXECUTION_ID}"}},
        UpdateExpression="SET claim_expires_at = :expired",
        ExpressionAttributeValues={":expired": {"N": "1"}},
    )

    reclaimed = _claim(execution_store)

    assert reclaimed.acquired
    assert reclaimed.attempt == 2


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


def test_the_target_comes_from_the_completed_analysis_playbook(store):
    execution_store, ddb = store
    _put_completed_session(ddb)
    _put_playbook_span(ddb)

    target = execution_store.load_target(RCA_ID, ENGINE)

    assert target.playbook["playbook_id"] == "pb-1"
    assert target.playbook["execution_steps"] == PLAYBOOK_STEPS
    assert target.alarm_name == "VitalIngestFailure"
    assert target.metric_name == "Vital"


@pytest.mark.parametrize("state", ["ANALYZING", "FAILED", "CANCELLED", "OUTDATED"])
def test_an_unfinished_analysis_has_no_approvable_playbook(store, state):
    execution_store, ddb = store
    _put_completed_session(ddb, state=state)
    _put_playbook_span(ddb)

    with pytest.raises(ExecutionTargetUnavailableError):
        execution_store.load_target(RCA_ID, ENGINE)


def test_a_missing_playbook_is_reported_rather_than_guessed(store):
    execution_store, ddb = store
    _put_completed_session(ddb)

    with pytest.raises(ExecutionTargetUnavailableError):
        execution_store.load_target(RCA_ID, ENGINE)


def test_a_revised_playbook_becomes_the_basis_of_the_next_execution(store):
    execution_store, ddb = store
    _put_completed_session(ddb)
    _put_playbook_span(ddb)
    revised = {
        "playbook_id": "pb-1",
        "failure_type": "DB 커넥션 누수",
        "execution_steps": [
            {**PLAYBOOK_STEPS[0], "action": "api 서비스를 강제 재배포하고 30초 대기"},
        ],
    }
    execution_store.save_playbook_revision(RCA_ID, ENGINE, revised, execution_id=EXECUTION_ID)

    target = execution_store.load_target(RCA_ID, ENGINE)

    assert target.playbook["execution_steps"][0]["action"].endswith("30초 대기")


def test_only_one_retrospective_runs_per_execution(store):
    execution_store, ddb = store
    claim = _claim(execution_store)
    _put_completed_session(ddb)
    execution_store.update_state(
        EXECUTION_ID, rca_id=RCA_ID, state=ExecutionState.VERIFYING, claim_token=claim.claim_token
    )
    execution_store.update_state(
        EXECUTION_ID, rca_id=RCA_ID, state=ExecutionState.RESOLVED, claim_token=claim.claim_token
    )

    first = execution_store.claim_retrospective(EXECUTION_ID, rca_id=RCA_ID, claim_token=claim.claim_token)
    second = execution_store.claim_retrospective(EXECUTION_ID, rca_id=RCA_ID, claim_token=claim.claim_token)

    assert first
    assert not second


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
