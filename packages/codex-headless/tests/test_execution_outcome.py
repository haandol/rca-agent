import pytest

from codex_headless.services.execution_evidence import FailureClass
from codex_headless.services.execution_outcome import assemble_evidence, judge_resolution
from codex_headless.services.execution_state import (
    ExecutionState,
    InvalidExecutionTransitionError,
    assert_transition,
    enters_retrospective,
    is_terminal,
)

PLAYBOOK = {
    "playbook_id": "pb-1",
    "execution_steps": [
        {
            "step_id": "step-1",
            "intent": "커넥션 풀 회수",
            "action": "api 서비스를 강제 재배포",
            "success_criteria": "DatabaseConnections 가 20 이하로 복귀",
        },
        {
            "step_id": "step-2",
            "intent": "증상 지표 확인",
            "action": "VitalIngestFailure 지표 조회",
            "success_criteria": "VitalIngestFailure 가 0",
        },
    ],
}


def _records(*records: dict) -> list[dict]:
    return list(records)


def _resolved_records() -> list[dict]:
    return _records(
        {"type": "attempt", "step_id": "step-1", "command": "aws ecs update-service", "succeeded": True},
        {
            "type": "step_outcome",
            "step_id": "step-1",
            "success_criteria": "DatabaseConnections 가 20 이하로 복귀",
            "observation": "DatabaseConnections 12",
            "criteria_met": True,
        },
        {"type": "attempt", "step_id": "step-2", "command": "aws cloudwatch get-metric-data", "succeeded": True},
        {
            "type": "step_outcome",
            "step_id": "step-2",
            "success_criteria": "VitalIngestFailure 가 0",
            "observation": "0",
            "criteria_met": True,
        },
        {"type": "resolution", "observation": "증상 지표 정상", "resolved": True},
    )


def _assemble(records: list[dict]):
    return assemble_evidence(
        records,
        execution_id="exec-1",
        rca_id="rca-1",
        engine="codex-headless",
        playbook=PLAYBOOK,
    )


def test_observed_resolution_completes_the_execution():
    verdict = judge_resolution(_assemble(_resolved_records()), agent_succeeded=True)

    assert verdict.state is ExecutionState.RESOLVED


def test_a_missing_resolution_observation_is_not_assumed_to_be_resolved():
    records = [record for record in _resolved_records() if record["type"] != "resolution"]

    verdict = judge_resolution(_assemble(records), agent_succeeded=True)

    assert verdict.state is ExecutionState.UNRESOLVED
    assert "cannot be confirmed" in verdict.reason


def test_an_unobservable_result_is_not_recorded_as_resolved():
    records = [record for record in _resolved_records() if record["type"] != "resolution"]
    records.append(
        {
            "type": "resolution",
            "observation": "메트릭 조회 실패",
            "resolved": False,
            "unobservable_reason": "지표 반영이 지연되어 확정할 수 없음",
        }
    )

    evidence = _assemble(records)
    verdict = judge_resolution(evidence, agent_succeeded=True)

    assert verdict.state is ExecutionState.UNRESOLVED
    assert "unobservable" in evidence.resolution_observation


def test_a_step_that_missed_its_criteria_blocks_completion_even_if_the_agent_claims_resolution():
    records = _resolved_records()
    records[3] = {
        "type": "step_outcome",
        "step_id": "step-2",
        "success_criteria": "VitalIngestFailure 가 0",
        "observation": "VitalIngestFailure 4",
        "criteria_met": False,
    }

    verdict = judge_resolution(_assemble(records), agent_succeeded=True)

    assert verdict.state is ExecutionState.UNRESOLVED
    assert "step-2" in verdict.reason


@pytest.mark.parametrize("supplied_criteria", ["VitalIngestFailure 5 이하", None])
def test_a_model_supplied_criterion_cannot_replace_the_approved_criterion(supplied_criteria):
    records = _resolved_records()
    records[3] = {
        "type": "step_outcome",
        "step_id": "step-2",
        "success_criteria": supplied_criteria,
        "observation": "VitalIngestFailure 4",
        "criteria_met": True,
    }

    evidence = _assemble(records)
    verdict = judge_resolution(evidence, agent_succeeded=True)

    assert evidence.step("step-2").success_criteria == "VitalIngestFailure 가 0"
    assert evidence.step("step-2").resolved is False
    assert verdict.state is ExecutionState.UNRESOLVED
    assert "step-2" in verdict.reason


def test_an_attempted_step_without_any_observation_blocks_completion():
    records = [record for record in _resolved_records() if record.get("step_id") != "step-2"]
    records.append(
        {"type": "attempt", "step_id": "step-2", "command": "aws cloudwatch get-metric-data", "succeeded": True}
    )
    records.append({"type": "resolution", "observation": "정상", "resolved": True})

    verdict = judge_resolution(_assemble(records), agent_succeeded=True)

    assert verdict.state is ExecutionState.UNRESOLVED
    assert "no recorded observation" in verdict.reason


def test_an_agent_that_did_not_finish_fails_rather_than_resolving():
    verdict = judge_resolution(_assemble(_resolved_records()), agent_succeeded=False)

    assert verdict.state is ExecutionState.FAILED


def test_a_blocked_step_is_marked_manual_and_does_not_stop_the_rest():
    records = _records(
        {
            "type": "attempt",
            "step_id": "step-1",
            "command": "aws ecs delete-service",
            "succeeded": False,
            "blocked": True,
            "block_reason": "ecs delete-service is an irreversible operation",
            "failure_class": str(FailureClass.BLOCKED_DESTRUCTIVE),
        },
        {
            "type": "step_outcome",
            "step_id": "step-1",
            "success_criteria": "DatabaseConnections 가 20 이하로 복귀",
            "observation": "차단되어 수동 조치 필요",
            "criteria_met": False,
            "manual_action_required": True,
        },
        {"type": "attempt", "step_id": "step-2", "command": "aws cloudwatch get-metric-data", "succeeded": True},
        {
            "type": "step_outcome",
            "step_id": "step-2",
            "success_criteria": "VitalIngestFailure 가 0",
            "observation": "0",
            "criteria_met": True,
        },
        {"type": "resolution", "observation": "일부 절차가 수동 조치로 남음", "resolved": False},
    )

    evidence = _assemble(records)

    assert evidence.step("step-1").manual_action_required
    assert evidence.step("step-1").blocked
    assert evidence.step("step-2").succeeded
    assert judge_resolution(evidence, agent_succeeded=True).state is ExecutionState.UNRESOLVED


def test_declared_steps_appear_in_the_evidence_even_when_never_attempted():
    """시도되지 않은 절차가 사라지면 실행이 절차를 건너뛴 사실을 알 수 없다."""
    evidence = _assemble(_records({"type": "resolution", "observation": "아무것도 하지 않음", "resolved": False}))

    assert [step.step_id for step in evidence.steps] == ["step-1", "step-2"]
    assert evidence.attempted_step_count == 0


def test_skipped_step_cannot_resolve_even_when_global_resolution_claims_success():
    records = [record for record in _resolved_records() if record.get("step_id") != "step-2"]

    verdict = judge_resolution(_assemble(records), agent_succeeded=True)

    assert verdict.state is ExecutionState.UNRESOLVED
    assert "not attempted" in verdict.reason


def test_successful_criteria_requires_a_nonblank_observation():
    records = _resolved_records()
    records[1] = {
        "type": "step_outcome",
        "step_id": "step-1",
        "success_criteria": "DatabaseConnections 가 20 이하로 복귀",
        "observation": " ",
        "criteria_met": True,
    }

    verdict = judge_resolution(_assemble(records), agent_succeeded=True)

    assert verdict.state is ExecutionState.UNRESOLVED
    assert "no recorded observation" in verdict.reason


def test_global_resolution_requires_a_nonblank_observation():
    records = _resolved_records()
    records[-1] = {"type": "resolution", "observation": "", "resolved": True}

    verdict = judge_resolution(_assemble(records), agent_succeeded=True)

    assert verdict.state is ExecutionState.UNRESOLVED
    assert "nonblank" in verdict.reason


def test_evidence_assembly_redacts_credentials_from_recorded_commands():
    evidence = _assemble(
        _records(
            {
                "type": "attempt",
                "step_id": "step-1",
                "command": "aws rds modify-db-instance --master-user-password hunter2",
                "succeeded": True,
            }
        )
    )

    assert "hunter2" not in evidence.step("step-1").attempts[0].command


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ExecutionState.PENDING_APPROVAL, ExecutionState.EXECUTING),
        (ExecutionState.EXECUTING, ExecutionState.VERIFYING),
        (ExecutionState.VERIFYING, ExecutionState.RESOLVED),
        (ExecutionState.VERIFYING, ExecutionState.UNRESOLVED),
        (ExecutionState.EXECUTING, ExecutionState.FAILED),
        (ExecutionState.EXECUTING, ExecutionState.CANCELLED),
    ],
)
def test_allowed_transitions(current, target):
    assert_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # 관측 단계를 건너뛰고 해결로 갈 수 없다.
        (ExecutionState.EXECUTING, ExecutionState.RESOLVED),
        (ExecutionState.PENDING_APPROVAL, ExecutionState.RESOLVED),
        (ExecutionState.PENDING_APPROVAL, ExecutionState.VERIFYING),
        # 종료된 실행은 되살아나지 않는다.
        (ExecutionState.RESOLVED, ExecutionState.EXECUTING),
        (ExecutionState.FAILED, ExecutionState.EXECUTING),
        (ExecutionState.CANCELLED, ExecutionState.EXECUTING),
        (ExecutionState.UNRESOLVED, ExecutionState.RESOLVED),
    ],
)
def test_forbidden_transitions(current, target):
    with pytest.raises(InvalidExecutionTransitionError):
        assert_transition(current, target)


def test_only_resolved_executions_enter_the_retrospective():
    assert enters_retrospective(ExecutionState.RESOLVED)
    for state in (
        ExecutionState.UNRESOLVED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
        ExecutionState.EXECUTING,
        ExecutionState.VERIFYING,
        ExecutionState.PENDING_APPROVAL,
    ):
        assert not enters_retrospective(state)


def test_terminal_states_are_exactly_the_states_that_end_an_execution():
    assert {state for state in ExecutionState if is_terminal(state)} == {
        ExecutionState.RESOLVED,
        ExecutionState.UNRESOLVED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
    }
