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
        "aws logs get-log-events",
        "aws cloudwatch list-metrics",
        "aws cloudwatch describe-alarms",
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
    """Describe the execution-only fixed-window receipt without granting planner authority."""
    waiter = evaluate_command("aws cloudwatch wait alarm-exists --state-value OK")
    waiter_guidance = (
        "기존 CloudWatch waiter도 run_playbook_command로 사용할 수 있지만 실제 알람 이름과 리전을 "
        "명시한다. 이미 OK이면 즉시 끝날 수 있다. waiter 성공은 조치 후 두 구간의 완료나 복구 증거가 아니다."
        if waiter.allowed
        else f"현재 command gate는 CloudWatch waiter를 거부한다: {waiter.reason}. 거부를 우회하지 않는다."
    )
    return "\n".join(
        [
            "## 관측 대기와 새 데이터",
            "승인 실행 워커에는 `wait_for_post_action_metrics(step_id, action_step_id, metrics, "
            "failure_alarm_name, region, max_wait_seconds=300, latency_alarm_name='', "
            "completed_work_evidence=None)`가 있다. "
            "분석/Report에는 이 실행 도구가 없다. 승인된 현재 검증 step_id와 그보다 앞선 승인 조치 "
            "action_step_id를 전달한다. 임의 시각이나 셸 명령을 받는 도구가 아니다.",
            "앵커는 같은 실행 범위에서 action_step_id의 실제 ECS StopTask가 처음 성공한 서버 기록 "
            "ended_at이다. 실패한 조치·읽기 전용 명령·다른 실행·미승인 절차는 앵커가 될 수 없다. "
            "서버는 floor(epoch/60)*60+60부터 첫 두 개의 완결된 60초 구간과 요청을 조회 전에 고정한다. "
            "epoch는 ended_at의 UTC 초이며 정확한 분 경계에 끝나도 반드시 다음 분부터 두 구간이다. "
            "후속 조회·조치 재시도로 시각을 "
            "옮기거나 더 늦은 정상 구간을 고르지 않는다.",
            "먼저 현재 검증 step_id의 `run_playbook_command`로 `aws cloudwatch list-metrics`와 "
            "`aws cloudwatch describe-alarms`를 실행해 실제 이름·좌표·임계값을 서버 증거에 남긴다. "
            "metrics는 필수 attempts/failures 역할별 namespace, metric_name, dimensions(이름→값 매핑)를 "
            "담는 dict다. 지표는 승인된 현재 서비스의 같은 namespace·dimensions·region이어야 한다. "
            "관측한 좌표만 명시적으로 전달하고 서비스·메트릭 이름을 추측하지 않는다. failure_alarm_name과 "
            "failures 지표는 승인 success_criteria에 명시된 정확한 알람·실패 지표여야 한다. "
            "latency는 승인 기준이 지연 지표와 알람을 명시적으로 포함할 때만 추가한다. 이때 "
            "latency_alarm_name은 latency 지표와 실제로 일치해야 하며 threshold·statistic·unit은 "
            "실제 알람 메타데이터를 사용한다. 임의 임계값이나 통계를 전달하지 않는다. 지원하지 않는 "
            "알람 형식이나 확인되지 않은 좌표는 성공으로 처리하지 않는다. 승인 기준이 쓰기 성공·실패 0·"
            "알람 OK만 요구하면 latency 지표를 생략하고 latency_alarm_name=''을 사용한다. "
            "선택 지연 메타데이터의 부재로 이 정상 계획을 막거나 승인 기준을 추가·완화하지 않는다.",
            "도구의 모든 AWS 명령도 기존 CLI gate·감사 기록을 거친 읽기 전용 조회다. 각 고정 구간에 "
            "attempts Sum>0, failures Sum==0가 필요하다. 선택한 latency 검증에는 실제 쿼리 SampleCount>0과 "
            "알람 기준상 정상 지연이 필요하다. 정확한 실패 알람의 OK 상태도 별도로 필요하다.",
            "영수증의 attempts-failures는 항상 산술 차이다. successful_writes는 서버가 완료된 쓰기 집계 "
            "의미를 실제 증거에 연결한 경우에만 제공한다. completed_work_evidence는 선택 입력이며 실제 "
            "생산자/소스/관측에서 완료된 쓰기 시도와 실패의 집계 의미를 먼저 발견해야 한다. 이 입력은 "
            "record_index와 json_pointer만 가진 참조다. record_index는 실제 현재 실행 명령 증거의 "
            "인덱스 또는 'approved_context'이며 json_pointer는 해당 JSON 안에서 관측한 descriptor 위치다. "
            "참조를 확인할 수 없으면 생략한다. 소스 인용은 "
            "실제 명령 증거나 승인 문맥을 참조해야 한다. 이름만 보고 의미를 추측하거나 descriptor·소스·"
            "인용·증거·집계 수를 만들어내지 않는다. 증거가 없으면 이 입력을 생략하고 모델이 실제 쓰기 "
            "작업의 성공을 별도 관측한다. 산술 차이만으로 쓰기를 증명하지 않는다. 관측된 서비스에서 "
            "operation=ingest가 쓰기이고 operation=patient_vitals가 읽기임을 확인한 경우 그 구분을 따른다. "
            "이 이름들은 발견을 대체하는 고정 식별자가 아니다. 쿼리 읽기 성공은 쓰기 성공이 아니다.",
            "누락은 0이 아니다. 부분 데이터·오류·나쁜 값도 UTC 관측 시각과 실제 CLI/HTTP 출력과 함께 "
            "매 poll 기록에 보존한다. observedAt은 각 명령 결과가 돌아온 뒤의 시각이며 완결 여부도 이 "
            "시각으로 판정한다. 다른 지표가 누락되어도 완결 구간의 실패 값은 재시도 전에 최종 실패다. "
            "조치 이전·겹치는 구간과 진행 중 구간은 제외하며, nonfinite·중복·"
            "분 경계에 맞지 않는 데이터는 거부한다. 완결된 고정 구간에서 한 번이라도 비정상이 "
            "관측되면 실패는 최종이며 뒤의 정상 값으로 덮어쓰거나 정상까지 계속 기다리지 않는다.",
            "max_wait_seconds는 최대 300초이며 남은 기존 실행 예산과 현재 MCP timeout 360초 안에서만 "
            "데이터 도착을 기다린다. 서버가 짧은 취소 가능한 대기와 예산 내 명령을 수행하고 취소·claim을 "
            "계속 검사한다. 기존 명령 timeout·MCP timeout·실행 시간·취소·claim 경계를 늘리거나 우회하지 않는다. "
            "일반 sleep·셸 sleep·모델 busy-poll로 시간을 채우지 않는다. 같은 검증 step_id의 동일 요청 "
            "재호출은 기존 최종 영수증을 반환하고, 다른 인자·앵커는 거부된다. 재호출로 구간이나 "
            "대기 예산을 초기화하지 않는다.",
            "과거 로그 조회는 현재 사고의 실제 시간 범위를 --start-time/--end-time으로 제한한다. "
            "관측으로 소유자 로그 스트림을 알면 `aws logs get-log-events`로 그 스트림을 먼저 조회하고, "
            "`aws logs filter-log-events`가 필요해도 같은 시간 범위와 알려진 스트림을 사용한다. "
            "시간·그룹·스트림·필터를 만들지 않는다. 전체 이력의 무제한 검색을 피하되 페이지 토큰을 "
            "따라 필요한 페이지를 수집하고 조회 범위·페이지 완결 여부를 기록한다. 예산 내 완료하지 "
            "못한 페이지나 출력 잘림은 관측 부족으로 남기며 조용히 건수 제한하거나 부재로 해석하지 않는다.",
            waiter_guidance,
            "영수증만으로 전체 RESOLVED가 자동 확정되지 않으며 승인 success_criteria도 바뀌지 않는다. "
            "모델은 정확히 같은 소유자의 해제·롤백 기록을 조작 기록에 연결해 별도로 확인하고 모든 승인 "
            "기준에 대한 record_step_outcome과 record_resolution을 기록한다. 필수 관측이 부족하면 "
            "criteria_met=false, resolved=false로 남긴다. timeout·자연 만료·운영자의 별도 중지를 "
            "승인 실행의 복구로 인정하지 않는다.",
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
