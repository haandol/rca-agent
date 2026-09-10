from pathlib import Path
from unittest.mock import patch

import pytest

from headless_codex.ports.dto.models import AlarmContext
from headless_codex.services.prompt_builder import build_prompt

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@patch("headless_codex.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
def test_build_prompt_contains_alarm_info():
    alarm = AlarmContext(
        alarm_name="TestAlarm",
        state_reason="Threshold crossed",
        state_change_time="2024-01-01T00:00:00Z",
        region="us-east-1",
        metric_name="CPUUtilization",
        namespace="AWS/ECS",
        dimensions={"ClusterName": "prod"},
    )
    prompt = build_prompt(alarm)

    assert "TestAlarm" in prompt
    assert "Threshold crossed" in prompt
    assert "CPUUtilization" in prompt
    assert "ClusterName=prod" in prompt


@patch("headless_codex.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
def test_build_prompt_handles_missing_fields():
    alarm = AlarmContext(alarm_name="MinimalAlarm")
    prompt = build_prompt(alarm)

    assert "MinimalAlarm" in prompt
    assert "N/A" in prompt


@patch("headless_codex.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
def test_build_prompt_resolves_section_includes():
    alarm = AlarmContext(alarm_name="IncludeTest")
    prompt = build_prompt(alarm)

    assert "{{include: ./sections/" not in prompt
    for marker in (
        "scoping.json",
        "hypotheses.json",
        "validation-{N}.json",
        "playbook.json",
        "1단계: RCA 전문 에이전트",
        "2단계: Report 전문 에이전트",
        "핵심 원칙",
    ):
        assert marker in prompt, f"missing: {marker}"


@pytest.mark.parametrize("role", ["rca", "report"])
def test_specialist_prompts_preserve_shared_contracts_without_orchestrator_commands(role):
    prompt = build_prompt(
        AlarmContext(
            alarm_name="SharedAlarm",
            state_change_time="2026-09-09T00:00:00Z",
            state_reason="[signal-1] current evidence",
        ),
        role=role,
    )

    assert "{{include:" not in prompt
    assert "spawn_agent" not in prompt
    assert "메인은" not in prompt
    assert "SharedAlarm" in prompt
    assert "2026-09-09T00:00:00Z" in prompt
    assert "[signal-1] current evidence" in prompt
    assert "다른 에이전트를 위임하지 않는다" in prompt
    assert "이 워커는 비대화형이다" in prompt
    # Exact shared source inclusion prevents the trimmed views silently losing a rule.
    for source in (
        "core/artifacts-overview.md",
        "core/principles.md",
        "artifacts/scoping.md",
        "artifacts/hypotheses.md",
        "artifacts/validation.md",
    ):
        assert (PROMPTS_DIR / "sections" / source).read_text() in prompt


def test_role_specific_prompts_keep_report_schemas_and_remove_unowned_work():
    alarm = AlarmContext(alarm_name="RoleContract")
    rca = build_prompt(alarm, role="rca")
    report = build_prompt(alarm, role="report")
    orchestrator = build_prompt(alarm)
    playbook_schema = (PROMPTS_DIR / "sections/artifacts/playbook.md").read_text()
    assert playbook_schema in report
    assert playbook_schema not in rca
    assert (PROMPTS_DIR / "sections/roles/rca.md").read_text() in rca
    assert (PROMPTS_DIR / "sections/roles/report.md").read_text() in report
    assert "두 파일의 저장 응답이 모두 `ok: true`" in report
    assert "서버 `decision`" in rca
    assert "분석 산출물을 다시 작성하거나 저장하지 않는다" in report
    assert len(rca) + len(report) < 2 * len(orchestrator)
