"""Describe the existing execution gate to planners without granting execution authority."""

from headless_codex.services.command_gate import evaluate_command
from headless_codex.services.destructive_actions import SELF_CONTROL_OPERATIONS


def execution_capability_facts() -> list[dict]:
    """Classify representative operations with the same gate used by the execution worker."""
    commands = [
        "aws ecs describe-tasks",
        "aws ecs describe-task-definition",
        "aws ecs list-tags-for-resource",
        "aws ecs stop-task",
        "aws ecs update-service",
        "aws logs filter-log-events",
        "aws cloudwatch get-metric-statistics",
        "aws cloudwatch wait alarm-exists --state-value OK",
        "aws ec2 terminate-instances",
        "psql -c ROLLBACK",
        "sh -c true",
        *(f"aws {service} {operation}" for service, operation in sorted(SELF_CONTROL_OPERATIONS)),
    ]
    return [
        {"operation": command, "allowed": verdict.allowed, "reason": verdict.reason}
        for command in commands
        for verdict in [evaluate_command(command)]
    ]


def render_observation_wait_guidance() -> str:
    """Describe bounded CLI waiting and the fresh evidence it cannot guarantee by itself."""
    # Verified against AWS CLI 2.34.20's installed waiter model and the primary reference:
    # https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/wait/alarm-exists.html
    waiter = evaluate_command("aws cloudwatch wait alarm-exists --state-value OK")
    if not waiter.allowed:
        return (
            "## 관측 대기와 새 데이터\n현재 command gate는 CloudWatch waiter를 거부한다: "
            f"{waiter.reason}. 우회하거나 새 sleep 도구를 가정하지 않는다. "
            "필수 사후 관측이 부족하면 criteria_met=false, resolved=false로 남긴다."
        )
    return "\n".join(
        [
            "## 관측 대기와 새 데이터",
            "일반 sleep 도구는 없다. 실행 워커는 기존 run_playbook_command 안에서 읽기 전용 "
            "CloudWatch waiter를 사용할 수 있다. 실제 확인한 알람 이름 하나와 리전을 지정해 "
            "`aws cloudwatch wait alarm-exists --alarm-names <확인된이름> --state-value OK --region <확인된리전>`"
            "으로 기다린다. 알람 이름을 생략해 무관한 OK 알람을 기다리지 않는다.",
            "이 waiter는 DescribeAlarms를 5초 간격, 최대 40회 확인하며 충족하지 못하면 255로 끝난다. "
            "기존 명령 timeout·MCP timeout·실행 시간·취소·claim 경계를 늘리거나 우회하지 않는다. "
            "별도 --waiter-delay/--max-attempts 옵션이나 셸 sleep을 만들지 않는다.",
            "waiter 성공은 알람 상태 관측일 뿐 복구나 두 개의 새로운 60초 구간을 보장하지 않는다. "
            "이미 OK이면 즉시 끝날 수 있다. success_criteria가 두 60초 구간을 요구하면 "
            "성공 뒤 조치 완료 시각 이후의 완결된 두 60초 구간을 "
            "CloudWatch 지표로 따로 확인한다. 조치가 분 중간이면 다음 분 경계부터 두 구간을 사용한다.",
            "get-metric-statistics 결과를 Timestamp 순으로 검토한다. StartTime 분 단위 내림으로 포함된 "
            "조치 이전/겹치는 구간, 진행 중 구간, 누락된 datapoint는 성공 근거에서 제외한다. "
            "오류 0뿐 아니라 실제 성공 요청이 있어야 하며, 조작 기록과 동일 소유자의 해제·롤백 기록을 연결한다.",
            "waiter가 일찍 끝나 새 지표가 부족하면 즉시 waiter/describe/metric 조회를 반복하며 시간을 "
            "소비하지 않는다. 현재 도구에는 정확한 두 구간 경과를 보장하는 대기가 없으므로 "
            "관측 부족 사유를 기록하고 criteria_met=false, resolved=false로 남긴다. "
            "timeout·255·자연 만료·운영자의 별도 중지를 승인 실행의 복구로 인정하지 않는다.",
        ]
    )


def render_execution_capabilities() -> str:
    """Render policy facts, evidence prerequisites and approval order without invented targets."""
    rows = [
        f"- `{fact['operation']}`: {'허용' if fact['allowed'] else '거부'}"
        + (f" ({fact['reason']})" if fact["reason"] else "")
        for fact in execution_capability_facts()
    ]
    return "\n".join(
        [
            "## 별도 승인 실행 워커의 현재 명령 정책",
            "아래는 기존 command gate의 정적 판정이며 명령 실행 예시나 리소스·IAM 권한 확인이 아니다.",
            "분석/Report에는 이 실행 도구가 없다. 실제 명령과 인자는 승인 후 서버가 다시 검사한다.",
            *rows,
            "직접 SQL, 셸, ECS exec, 태스크 정의 등록이나 새 태스크 실행으로 우회하지 않는다.",
            "플레이북은 읽기 전용 소유자 발견 → 사용자 승인된 조작 → 사후 건강 관측 순서로 구성한다.",
            "서비스 이름이나 DB PID만으로 ECS 소유 태스크를 추측하지 않는다. 현재 차단자와 소유자, "
            "리소스 식별자·리전·작업 수명·태그를 증거로 연결하고 조작 직전 다시 확인한다.",
            "stop-task가 허용되더라도 소유권과 롤백 경로가 확인된 독립 실행 태스크에만 제안한다. "
            "일반적인 영구 인스턴스 종료와 구분하며, 임의 서비스 태스크 중지는 제안하지 않는다.",
            "소유자·실행 능력을 확인할 수 없으면 수동 에스컬레이션으로 남긴다. 존재하지 않는 "
            "스케줄러·환경 플래그·명령 인자·태스크를 만들지 않는다. 작업 만료나 관측 부재를 복구로 보지 않는다.",
            "표의 조회 능력은 승인 실행 워커의 능력이다. RCA는 자기 읽기 전용 MCP 도구만 사용하며, "
            "제공 관측 전용 model-eval에서는 실제 조회를 하지 않는다.",
            render_observation_wait_guidance(),
        ]
    )
