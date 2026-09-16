"""Retrospective guidance must attest no-change reviews through the existing save contract."""

import json
from pathlib import Path

from headless_codex import retrospective_mcp_server
from headless_codex.ports.interfaces.execution_store import ExecutionTarget
from headless_codex.services.execution_prompt import build_retrospective_prompt

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_analyst_require_persisted_no_change_attestation():
    """No-change review uses an empty update with rationale, not a new model-owned status."""
    target = ExecutionTarget(rca_id="rca", engine="headless-codex", alarm_name="alarm", playbook={})
    runtime = build_retrospective_prompt(
        target,
        execution_id="exec",
        evidence_key="executions/rca/exec/evidence.json",
        approved_playbook_key="approvals/rca/exec/playbook.json",
    )
    analyst = (PACKAGE_ROOT / "harness/retrospective/agents/retrospective-analyst.md").read_text()
    for guidance in (runtime, analyst):
        assert "`binding`·`request`·초기 알람 스냅샷은 고정 입력" in guidance
        assert "같은 대상·역할의 가장 늦은 실제 poll" in guidance
        assert "`stdout` 본문을 마지막 페이지까지 읽고" in guidance
        assert "terminal과 모순되면 그 모순을" in guidance
        assert "poll pointer와 관측 시각을 명시" in guidance
        assert "`complete=true`는 선택한 저장된 JSON 값의 페이지가 끝났다는 뜻일 뿐" in guidance
        assert "`stdout_truncated`·`projection_omissions`" in guidance
        assert "복원되지 않으며 여전히 알 수 없다" in guidance
        assert "오류·사건의 부재나 성공의 증거로 해석하지 않는다" in guidance
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


def test_documented_empty_update_is_accepted_and_persisted_by_existing_mcp(monkeypatch, tmp_path, retrospective_reads):
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
