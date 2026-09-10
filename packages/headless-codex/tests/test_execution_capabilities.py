"""Policy-derived planner context must not grant capabilities or invent target evidence."""

from headless_codex.ports.dto.models import AlarmContext
from headless_codex.services import execution_capabilities
from headless_codex.services.command_gate import GateVerdict, evaluate_command
from headless_codex.services.prompt_builder import build_prompt


def test_capability_facts_match_execution_gate_and_preserve_denials():
    """Reversible task stop differs from permanently terminating instances and escape commands."""
    facts = {fact["operation"]: fact for fact in execution_capabilities.execution_capability_facts()}
    assert facts["aws ecs stop-task"]["allowed"]
    for command in (
        "psql -c ROLLBACK",
        "sh -c true",
        "aws ecs execute-command",
        "aws ecs register-task-definition",
        "aws ecs run-task",
        "aws ecs start-task",
        "aws ec2 terminate-instances",
    ):
        assert not facts[command]["allowed"]
        assert facts[command]["reason"] == evaluate_command(command).reason


def test_capability_renderer_uses_live_policy_result_not_an_independent_allowlist(monkeypatch):
    """A gate refusal must appear in planner facts rather than remain hardcoded as allowed."""
    monkeypatch.setattr(
        execution_capabilities,
        "evaluate_command",
        lambda _command: GateVerdict(allowed=False, reason="policy refusal"),
    )
    assert not any(fact["allowed"] for fact in execution_capabilities.execution_capability_facts())
    assert "`aws ecs stop-task`: 거부 (policy refusal)" in execution_capabilities.render_execution_capabilities()


def test_both_analysis_and_report_receive_identical_policy_and_ownership_conditions():
    """Shared capability facts do not imply that the analysis role can execute commands."""
    facts = execution_capabilities.render_execution_capabilities()
    for role in ("rca", "report"):
        prompt = build_prompt(AlarmContext(alarm_name="symptom"), role=role)
        assert facts in prompt
        assert "분석/Report에는 이 실행 도구가 없다" in prompt
        assert "읽기 전용 소유자 발견 → 사용자 승인된 조작 → 사후 건강 관측" in prompt
        assert "현재 차단자와 소유자" in prompt
        assert "작업 만료나 관측 부재를 복구로 보지 않는다" in prompt


def test_waiter_is_an_existing_read_only_command_not_a_freshness_guarantee():
    """Waiter policy and planner guidance preserve timeout and non-resolution boundaries."""
    command = "aws cloudwatch wait alarm-exists --alarm-names observed --state-value OK --region us-east-1"
    assert evaluate_command(command).allowed
    guidance = execution_capabilities.render_observation_wait_guidance()
    assert "이미 OK이면 즉시 끝날 수 있다" in guidance
    assert "조치가 분 중간이면 다음 분 경계부터 두 구간" in guidance
    assert "criteria_met=false, resolved=false" in guidance
    assert "경계를 늘리거나 우회하지 않는다" in guidance
    assert "즉시 waiter/describe/metric 조회를 반복" in guidance
