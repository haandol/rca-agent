"""The assembled RCA role must expose the server's existing planning limits."""

from headless_codex.ports.dto.models import AlarmContext
from headless_codex.services.analysis_contract import MAX_VALIDATION_LOOPS, TERMINATION_THRESHOLD
from headless_codex.services.prompt_builder import build_prompt


def test_rca_prompt_exposes_early_report_and_remaining_round_context():
    prompt = build_prompt(AlarmContext(alarm_name="PlanningContext"), role="rca")
    assert f"최고 신뢰도가 {TERMINATION_THRESHOLD} 이상이면 즉시 `REPORT`" in prompt
    assert f"전체 validation은 최대 {MAX_VALIDATION_LOOPS}회" in prompt
    assert f"max(0, {MAX_VALIDATION_LOOPS}-N)" in prompt
    assert "`PENDING`/`NEEDS_INVESTIGATION` 가설은 서버가 `CLOSED`" in prompt
    assert "기각 판정을 뜻하지 않는다" in prompt
