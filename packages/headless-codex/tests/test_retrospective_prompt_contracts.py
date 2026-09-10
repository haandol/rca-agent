"""Retrospective guidance must attest no-change reviews through the existing save contract."""

import json
from pathlib import Path

import pytest

from headless_codex import retrospective_mcp_server
from headless_codex.ports.interfaces.execution_store import ExecutionTarget
from headless_codex.services.execution_evidence import CommandAttempt, ExecutionEvidence
from headless_codex.services.execution_prompt import build_retrospective_prompt

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_analyst_require_persisted_no_change_attestation():
    """No-change review uses an empty update with rationale, not a new model-owned status."""
    target = ExecutionTarget(rca_id="rca", engine="headless-codex", alarm_name="alarm", playbook={})
    evidence = ExecutionEvidence(execution_id="exec", rca_id="rca", playbook_id="pb")
    runtime = build_retrospective_prompt(target, evidence, execution_id="exec")
    analyst = (PACKAGE_ROOT / "harness/retrospective/agents/retrospective-analyst.md").read_text()
    for guidance in (runtime, analyst):
        assert 'save_playbook_update(update_json="{}", rationale=' in guidance
        assert "비어 있지 않은 근거" in guidance
        assert "`ok: true`" in guidance
        assert "서버가 기존 NO_CHANGE 상태를 판정" in guidance
        assert "status나 verification_status를 갱신안에 넣지 않는다" in guidance
        assert "저장하지 않고" not in " ".join(guidance.split())
        assert "갱신안을 저장하지" not in " ".join(guidance.split())
    orchestrator = (PACKAGE_ROOT / "harness/retrospective/AGENTS.md").read_text()
    assert "빈 갱신 `{}`" in orchestrator
    assert "응답만 남기고 저장을 생략한 회고는 완료가 아니다" in orchestrator


def test_documented_empty_update_is_accepted_and_persisted_by_existing_mcp(monkeypatch, tmp_path):
    """The documented NO_CHANGE attestation matches the real MCP payload without adding fields."""
    path = tmp_path / "retrospective.json"
    monkeypatch.setattr(retrospective_mcp_server, "_target_path", lambda: path)
    rationale = "Observed execution met every criterion with no procedure defects."
    response = json.loads(retrospective_mcp_server.save_playbook_update(update_json="{}", rationale=rationale))
    assert response["ok"]
    assert json.loads(path.read_text()) == {"update": {}, "rationale": rationale}


def test_no_change_attestation_still_requires_a_nonempty_rationale(monkeypatch, tmp_path):
    """An empty proposed diff does not excuse missing review evidence."""
    path = tmp_path / "retrospective.json"
    monkeypatch.setattr(retrospective_mcp_server, "_target_path", lambda: path)
    response = json.loads(retrospective_mcp_server.save_playbook_update(update_json="{}", rationale=" "))
    assert not response["ok"]
    assert not path.exists()


def test_large_retrospective_evidence_is_valid_bounded_json_with_final_resolution():
    """Budgeting must preserve final resolution and step identities instead of slicing JSON text."""
    target = ExecutionTarget(rca_id="rca", engine="headless-codex", alarm_name="alarm", playbook={})
    evidence = ExecutionEvidence(
        execution_id="exec",
        rca_id="rca",
        playbook_id="pb",
        final_state="RESOLVED",
        resolution_confirmed=True,
        resolution_observation="sigterm rollback verified with two completed post-action periods",
    )
    for index in range(12):
        evidence.record_attempt(
            CommandAttempt(
                step_id=f"step-{index}",
                command="aws cloudwatch get-metric-statistics",
                arguments={},
                exit_status="0",
                succeeded=True,
                attempt_index=1,
                observation="large-observation-" + "x" * 12000,
                recorded_at="2026-09-10T10:00:00Z",
            )
        )
    prompt = build_retrospective_prompt(target, evidence, execution_id="exec")
    evidence_json = prompt.split("## 실행 증거\n\n```json\n")[1].split("\n```")[0]
    parsed = json.loads(evidence_json)
    assert len(evidence_json) <= 60000
    assert parsed["final_state"] == "RESOLVED"
    assert parsed["resolution_observation"] == evidence.resolution_observation
    assert parsed["resolution_confirmed"] is True
    assert {step["step_id"] for step in parsed["steps"]} == {f"step-{index}" for index in range(12)}
    assert parsed["projection"]["output_previews_omitted"] is True
    assert parsed["projection"]["persisted_evidence_unchanged"] is True
    assert all(step["attempts"][0]["recorded_at"] == "2026-09-10T10:00:00Z" for step in parsed["steps"])
    assert all(len(step.attempts[0].observation) > 12000 for step in evidence.steps)


def test_retrospective_budget_failure_propagates_instead_of_dropping_resolution():
    """The existing retrospective failure path receives an error when essential metadata cannot fit."""
    target = ExecutionTarget(rca_id="rca", engine="headless-codex", alarm_name="alarm", playbook={})
    evidence = ExecutionEvidence(
        execution_id="exec",
        rca_id="rca",
        playbook_id="pb",
        resolution_confirmed=True,
        final_state="RESOLVED",
        resolution_observation="essential outcome " * 4000,
    )
    with pytest.raises(ValueError, match="metadata exceeds JSON budget"):
        build_retrospective_prompt(target, evidence, execution_id="exec")
