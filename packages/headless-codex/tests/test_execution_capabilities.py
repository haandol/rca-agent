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
        "aws cloudwatch list-metrics",
        "aws cloudwatch describe-alarms",
        "aws logs get-log-events",
        "aws logs filter-log-events",
    ):
        assert facts[command]["allowed"] == evaluate_command(command).allowed
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
    assert "정확한 분 경계에 끝나도 반드시 다음 분" in guidance
    assert "criteria_met=false, resolved=false" in guidance
    assert "경계를 늘리거나 우회하지 않는다" in guidance
    assert "현재 도구에는 정확한 두 구간 경과를 보장하는 대기가 없으므로" not in guidance


def test_fixed_wait_guidance_binds_approved_steps_to_server_action_time_and_immutable_bins():
    guidance = execution_capabilities.render_observation_wait_guidance()
    for contract in (
        "승인된 현재 검증 step_id",
        "그보다 앞선 승인 조치 action_step_id",
        "같은 실행 범위",
        "실제 ECS StopTask가 처음 성공한 서버 기록 ended_at",
        "실패한 조치·읽기 전용 명령·다른 실행·미승인 절차",
        "floor(epoch/60)*60+60",
        "첫 두 개의 완결된 60초 구간과 요청을 조회 전에 고정",
        "다른 인자·앵커는 거부",
        "기존 최종 영수증",
        "대기 예산을 초기화하지 않는다",
    ):
        assert contract in guidance


def test_fixed_wait_guidance_requires_recorded_discovery_and_actual_alarm_contract():
    guidance = execution_capabilities.render_observation_wait_guidance()
    for contract in (
        "별도 선행 commands 단계에 승인된 `run_playbook_command`",
        "`aws cloudwatch list-metrics`",
        "`aws cloudwatch describe-alarms`",
        "필수 attempts/failures",
        "namespace, metric_name, dimensions",
        "승인된 현재 서비스의 같은 namespace·dimensions·region",
        "승인 success_criteria에 명시된 정확한 알람·실패 지표",
        "threshold·statistic·unit",
        "실제 알람 메타데이터",
        "latency는 승인 기준이 지연 지표와 알람을 명시적으로 포함할 때만",
        "latency 지표를 생략하고 latency_alarm_name=''",
        "기존 CLI gate·감사 기록",
    ):
        assert contract in guidance


def test_fixed_wait_guidance_preserves_bad_observations_budget_and_write_proof():
    guidance = execution_capabilities.render_observation_wait_guidance()
    for contract in (
        "attempts Sum>0",
        "failures Sum==0",
        "실제 쿼리 SampleCount>0",
        "attempts-failures는 항상 산술 차이",
        "successful_writes는 서버가",
        "completed_work_evidence",
        "실제 명령 증거나 승인 문맥",
        "실제 쓰기 작업의 성공을 별도 관측",
        "쿼리 읽기 성공은 쓰기 성공이 아니다",
        "누락은 0이 아니다",
        "UTC 관측 시각과 실제 CLI/HTTP 출력",
        "observedAt은 각 명령 결과가 돌아온 뒤",
        "다른 지표가 누락되어도 완결 구간의 실패 값은 재시도 전에 최종 실패",
        "nonfinite·중복",
        "실패는 최종",
        "최대 900초",
        "남은 기존 실행 예산",
        "MCP timeout 1200초",
        "취소·claim을 계속 검사",
        "모델 busy-poll",
        "전체 RESOLVED가 자동 확정되지 않으며",
        "같은 소유자의 해제·롤백",
        "criteria_met=false, resolved=false",
    ):
        assert contract in guidance


def test_waiter_gate_refusal_does_not_hide_fixed_wait_or_waive_command_gate(monkeypatch):
    monkeypatch.setattr(
        execution_capabilities,
        "evaluate_command",
        lambda _command: GateVerdict(allowed=False, reason="policy refusal"),
    )
    guidance = execution_capabilities.render_observation_wait_guidance()
    assert "CloudWatch waiter를 거부한다: policy refusal" in guidance
    assert "wait_for_post_action_metrics" in guidance
    assert "기존 CLI gate·감사 기록" in guidance
    assert "거부를 우회하지 않는다" in guidance


def test_log_discovery_guidance_keeps_incident_bounds_and_pagination():
    """History-query narrowing must retain the evidence-completeness obligation."""
    guidance = execution_capabilities.render_observation_wait_guidance()
    for term in (
        "--start-time/--end-time",
        "소유자 로그 스트림",
        "aws logs get-log-events",
        "aws logs filter-log-events",
        "페이지 토큰",
        "페이지 완결 여부",
        "관측 부족",
    ):
        assert term in guidance
