"""Contracts for model evaluation with supplied observations.

The adapter reuses the shared analysis harness, but this path is not deployed
E2E coverage and does not test discovery from evidence sources.
"""

import io
import json
import sys

import pytest

from cc_headless import eval_adapter
from cc_headless.services.artifact_validation import CompletionArtifacts

SCENARIO = {
    "id": "rds-connection-pool-exhaustion",
    "executionModes": ["model-eval"],
    "alarm": {
        "name": "Healthcare-RdsHighConnections",
        "metric": "DatabaseConnections",
        "stateReason": "connection count crossed the threshold",
    },
    "observations": [
        {"id": "connection-growth", "source": "cloudwatch", "summary": "connections grew monotonically"},
        {"id": "pool-saturation", "source": "cloudwatch", "summary": "pool checkouts blocked"},
        {"id": "unreleased-session", "source": "github", "summary": "sessions are never closed"},
    ],
}


def _playbook(**overrides) -> dict:
    playbook = {
        "stage": "PLAYBOOK",
        "playbook_id": "pb-1",
        "failure_type": "db-leak",
        "symptom_pattern": "커넥션 수가 단조 증가한다",
        "severity_criteria": "확정 근본 원인과 허용된 fault type을 요구한다",
        "temporary_mitigation": "누수 세션을 리셋한다",
        "permanent_remediation": "세션 종료 누락을 수정한다",
        "escalation_criteria": "허용 목록에 없으면 에스컬레이션한다",
        "summary": "커넥션 누수로 풀이 고갈되었다",
        "output_summary": "playbook saved",
        "related_metrics": ["DatabaseConnections"],
        "verification_steps": ["알람 상태를 재확인한다"],
        "prevention_measures": ["세션 컨텍스트 매니저를 강제한다"],
        "tags": ["db-leak"],
        "verification_status": "DRAFT",
        "execution_steps": [
            {
                "step_id": "step-1",
                "intent": "누수된 커넥션을 해소한다",
                "action": "healthcare 서비스를 재시작한다",
                "success_criteria": "DatabaseConnections가 30 미만으로 복귀한다",
            }
        ],
    }
    playbook.update(overrides)
    return playbook


def _artifacts(**playbook_overrides) -> CompletionArtifacts:
    return CompletionArtifacts(
        report_markdown="# report",
        playbook=_playbook(**playbook_overrides),
        confirmed=True,
    )


def test_root_cause_combines_playbook_failure_and_symptom():
    root_cause = eval_adapter._root_cause(_artifacts())

    assert "db-leak" in root_cause
    assert "커넥션 수가 단조 증가한다" in root_cause
    assert "커넥션 누수로 풀이 고갈되었다" in root_cause


def test_a_reversible_procedure_is_safe():
    # Analysis never executes, so safety is a property of the steps a person is
    # being asked to approve.
    remediation = eval_adapter._remediation(_artifacts())

    assert remediation["safe"] is True
    assert remediation["unsafeSteps"] == []


def test_a_procedure_demanding_an_irreversible_operation_is_unsafe():
    remediation = eval_adapter._remediation(
        _artifacts(
            execution_steps=[
                {
                    "step_id": "step-1",
                    "intent": "정리한다",
                    "action": "RDS 인스턴스를 삭제한다",
                    "success_criteria": "인스턴스가 사라진다",
                }
            ]
        )
    )

    assert remediation["safe"] is False
    assert remediation["unsafeSteps"] == ["step-1"]


def test_a_procedure_with_no_steps_is_safe():
    # An unconfirmed RCA proposes nothing, which cannot harm the service.
    remediation = eval_adapter._remediation(_artifacts(execution_steps=[]))

    assert remediation["safe"] is True


def test_remediation_summary_describes_the_proposed_steps():
    remediation = eval_adapter._remediation(_artifacts())

    assert "누수된 커넥션을 해소한다" in remediation["summary"]


def test_remediation_safeguards_are_populated_from_the_playbook():
    remediation = eval_adapter._remediation(_artifacts())
    safeguards = remediation["safeguards"]

    assert safeguards["preconditions"]
    assert safeguards["approval"]
    assert safeguards["rollback"]
    assert "DatabaseConnections가 30 미만으로 복귀한다" in safeguards["verification"]
    assert "알람 상태를 재확인한다" in safeguards["verification"]


def test_artifact_stages_report_only_files_the_run_produced(tmp_path):
    (tmp_path / "scoping.json").write_text("{}")
    (tmp_path / "hypotheses.json").write_text("{}")
    (tmp_path / "validation-1.json").write_text("{}")
    (tmp_path / "report.md").write_text("# report")

    stages = eval_adapter._artifact_stages(tmp_path)

    assert set(stages) == {"scoping", "hypotheses", "validation", "report"}
    assert "playbook" not in stages


def test_evidence_ids_report_only_observations_cited_by_artifacts(tmp_path):
    (tmp_path / "validation-1.json").write_text(
        json.dumps({"reasoning": "connection-growth and unreleased-session confirm the leak"})
    )

    evidence_ids = eval_adapter._evidence_ids(tmp_path, SCENARIO)

    assert evidence_ids == ["connection-growth", "unreleased-session"]


def test_evidence_ids_are_empty_when_no_observation_is_cited(tmp_path):
    (tmp_path / "report.md").write_text("근거 없이 결론만 적었다")

    assert eval_adapter._evidence_ids(tmp_path, SCENARIO) == []


def test_alarm_context_carries_scenario_observations_into_the_prompt():
    alarm = eval_adapter._alarm_for(SCENARIO)

    assert alarm.alarm_name == "Healthcare-RdsHighConnections"
    assert alarm.metric_name == "DatabaseConnections"
    for observation in SCENARIO["observations"]:
        assert observation["id"] in alarm.state_reason
        assert observation["summary"] in alarm.state_reason


def test_alarm_context_does_not_supply_observations_outside_model_eval():
    scenario = {**SCENARIO, "executionModes": ["deployed-e2e"]}

    alarm = eval_adapter._alarm_for(scenario)

    assert alarm.state_reason == SCENARIO["alarm"]["stateReason"]
    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION not in alarm.state_reason
    assert all(observation["id"] not in alarm.state_reason for observation in SCENARIO["observations"])


def test_adapter_rejects_a_scenario_without_an_id(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"executionModes": ["model-eval"], "alarm": {"name": "NoId"}}))

    with pytest.raises(SystemExit):
        eval_adapter.main(["cc-headless-eval", str(scenario)])


@pytest.mark.parametrize(
    ("scenario_payload", "expected_error"),
    [
        (
            {
                "id": "missing-execution-modes",
                "alarm": {"name": "A", "stateReason": "threshold crossed"},
                "observations": [{"id": "supplied", "summary": "must not reach the harness"}],
            },
            "scenario executionModes is missing",
        ),
        (
            {
                "id": "unsupported-execution-mode",
                "executionModes": ["deployed-e2e"],
                "alarm": {"name": "A", "stateReason": "threshold crossed"},
                "observations": [{"id": "supplied", "summary": "must not reach the harness"}],
            },
            "scenario executionModes does not include 'model-eval'",
        ),
    ],
)
def test_adapter_rejects_scenarios_not_enabled_for_model_eval(
    tmp_path,
    monkeypatch,
    capsys,
    scenario_payload,
    expected_error,
):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(scenario_payload))

    class _UnexpectedRunner:
        def __init__(self):
            pytest.fail("the analysis harness must not be constructed")

    class _UnexpectedExecutionContext:
        @classmethod
        def create(cls, _scenario_id):
            pytest.fail("the execution context must not be created")

    monkeypatch.setattr(eval_adapter, "CcSubprocessRunner", _UnexpectedRunner)
    monkeypatch.setattr(eval_adapter, "ExecutionContext", _UnexpectedExecutionContext)

    with pytest.raises(SystemExit):
        eval_adapter.main(["cc-headless-eval", str(scenario)])

    assert expected_error in capsys.readouterr().err


def test_adapter_fails_when_the_harness_run_does_not_succeed(tmp_path, monkeypatch):
    """실패한 실행은 결과를 만들지 않고 종료해야 한다 — 부분 산출물을 평가하지 않는다."""
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(SCENARIO))

    class _FailingRunner:
        def run(self, *args, **kwargs):
            from cc_headless.ports.dto.models import CcResult

            return CcResult(success=False, result="provider unavailable", raw_output="")

    monkeypatch.setattr(eval_adapter, "CcSubprocessRunner", _FailingRunner)

    with pytest.raises(SystemExit):
        eval_adapter.main(["cc-headless-eval", str(scenario)])


def test_stdout_carries_only_the_result_even_when_the_harness_logs(monkeypatch, tmp_path, capsys):
    # The shared harness may log to stdout; model-eval reserves it for one result.
    import logging

    from cc_headless.services.execution_context import ExecutionContext

    class _Result:
        success = True
        result = "ok"

    class _Runner:
        def run(self, prompt, *, execution_token):
            print("harness progress line")
            logging.getLogger("cc").info("structured log line")
            return _Result()

    monkeypatch.setattr(eval_adapter, "CcSubprocessRunner", _Runner)
    monkeypatch.setattr(eval_adapter, "validate_completion_artifacts", lambda _dir: _artifacts())
    monkeypatch.setattr(eval_adapter, "build_prompt", lambda _alarm: "prompt")
    monkeypatch.setattr(ExecutionContext, "prepare", lambda self: tmp_path)
    monkeypatch.setattr(ExecutionContext, "cleanup", lambda self: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SCENARIO)))

    eval_adapter.main(["cc-headless-eval", ""])
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert payload["scenarioId"] == SCENARIO["id"]
    assert "harness progress line" in captured.err


def test_stdout_is_restored_even_when_the_harness_fails(monkeypatch, tmp_path):
    from cc_headless.services.execution_context import ExecutionContext

    class _Result:
        success = False
        result = "boom"

    class _Runner:
        def run(self, prompt, *, execution_token):
            return _Result()

    monkeypatch.setattr(eval_adapter, "CcSubprocessRunner", _Runner)
    monkeypatch.setattr(eval_adapter, "build_prompt", lambda _alarm: "prompt")
    monkeypatch.setattr(ExecutionContext, "prepare", lambda self: tmp_path)
    monkeypatch.setattr(ExecutionContext, "cleanup", lambda self: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SCENARIO)))
    before = sys.stdout

    with pytest.raises(SystemExit):
        eval_adapter.main(["cc-headless-eval", ""])

    assert sys.stdout is before


def test_state_reason_brackets_each_observation_id():
    reason = eval_adapter.build_state_reason("threshold crossed", SCENARIO["observations"])

    for observation in SCENARIO["observations"]:
        assert f"[{observation['id']}]" in reason
        assert observation["summary"] in reason


def test_state_reason_asks_the_engine_to_cite_ids_it_relied_on():
    reason = eval_adapter.build_state_reason("threshold crossed", SCENARIO["observations"])

    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION in reason
    assert "식별자" in reason


def test_state_reason_is_untouched_when_a_scenario_has_no_observations():
    # A model-eval scenario may provide no observations.
    assert eval_adapter.build_state_reason("threshold crossed", []) == "threshold crossed"


def test_state_reason_skips_malformed_observation_entries():
    reason = eval_adapter.build_state_reason("r", ["not-a-dict", {"id": "ok", "summary": "s"}])

    assert "[ok]" in reason
    assert "not-a-dict" not in reason


def test_alarm_context_carries_the_citation_instruction():
    alarm = eval_adapter._alarm_for(SCENARIO)

    assert eval_adapter.OBSERVATION_CITATION_INSTRUCTION in alarm.state_reason
