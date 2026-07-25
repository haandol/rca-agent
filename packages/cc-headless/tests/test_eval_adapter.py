import json

import pytest

from cc_headless import eval_adapter
from cc_headless.services.artifact_validation import CompletionArtifacts

SCENARIO = {
    "id": "rds-connection-pool-exhaustion",
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
    }
    playbook.update(overrides)
    return playbook


def _artifacts(*, status: str = "SUCCEEDED", verification: str = "NORMALIZED") -> CompletionArtifacts:
    return CompletionArtifacts(
        report_markdown="# report",
        playbook=_playbook(),
        remediation={
            "stage": "REMEDIATION",
            "status": status,
            "fault_type": "db-leak",
            "verification": {"status": verification},
        },
        confirmed=True,
    )


def test_root_cause_combines_playbook_failure_and_symptom():
    root_cause = eval_adapter._root_cause(_artifacts())

    assert "db-leak" in root_cause
    assert "커넥션 수가 단조 증가한다" in root_cause
    assert "커넥션 누수로 풀이 고갈되었다" in root_cause


@pytest.mark.parametrize(
    ("status", "safe"),
    [("SUCCEEDED", True), ("NOT_ATTEMPTED", True), ("FAILED", False), ("BLOCKED", False)],
)
def test_remediation_safety_follows_server_owned_status(status, safe):
    remediation = eval_adapter._remediation(_artifacts(status=status))

    assert remediation["safe"] is safe


def test_remediation_safeguards_are_populated_from_the_playbook():
    remediation = eval_adapter._remediation(_artifacts())
    safeguards = remediation["safeguards"]

    assert safeguards["preconditions"]
    assert safeguards["approval"]
    assert safeguards["rollback"]
    assert "NORMALIZED" in safeguards["verification"]
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


def test_adapter_rejects_a_scenario_without_an_id(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"alarm": {"name": "NoId"}}))

    with pytest.raises(SystemExit):
        eval_adapter.main(["cc-headless-eval", str(scenario)])


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
