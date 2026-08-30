from pathlib import Path
from unittest.mock import patch

from codex_headless.ports.dto.models import AlarmContext
from codex_headless.services.prompt_builder import build_prompt

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@patch("codex_headless.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
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


@patch("codex_headless.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
def test_build_prompt_handles_missing_fields():
    alarm = AlarmContext(alarm_name="MinimalAlarm")
    prompt = build_prompt(alarm)

    assert "MinimalAlarm" in prompt
    assert "N/A" in prompt


@patch("codex_headless.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
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
