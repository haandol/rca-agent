# ADR 0006: SQS Visibility Timeout 기반 미완료 RCA 세션 자동 복구

Date: 2026-04-23
Updated: 2026-04-29

## Status

Accepted

## Context

ECS Fargate에서 실행되는 CC Headless는 다음 상황에서 재시작될 수 있다:

1. **서비스 배포**: `deploy-service.sh`로 force new deployment 실행 시 기존 태스크가 SIGTERM → DRAINING → 종료
2. **태스크 장애**: OOM, 헬스체크 실패, 인프라 이벤트 등으로 태스크가 비정상 종료
3. **수동 재시작**: ECS 콘솔이나 CLI에서 태스크를 직접 중지

기존 설계에서는 SQS 메시지를 처리 시작 시점에 즉시 삭제(`delete_message`)하고, DynamoDB에서 세션 상태를 관리했다. 이 경우:

- 처리 중 태스크가 죽으면 SQS 메시지는 이미 삭제되어 재처리할 수 없다
- DynamoDB에 진행 중 상태(`ANALYZING` 등)로 남은 세션이 영원히 미완료 상태가 된다
- 멱등성 키가 이미 기록되어 같은 알람이 다시 와도 중복으로 스킵된다

### 검토한 대안

1. **Heartbeat + Scan + Claim**: 서비스 시작 시 DynamoDB를 스캔하여 stale 세션을 찾고, 조건부 쓰기로 복구를 클레임하는 방식. 그러나 heartbeat이 CPU 쓰로틀이나 GC 지연으로 늦어지면 정상 진행 중인 세션을 잘못 stale로 판단할 위험이 있고, heartbeat 스레드 관리가 복잡하다.

2. **SQS Visibility Timeout 활용 (채택)**: 메시지 삭제를 처리 완료 시점으로 미뤄 SQS의 내장 재전달 메커니즘을 활용하는 방식.

## Decision

SQS 메시지 삭제 시점을 **처리 성공 후**로 변경하여, 실패/크래시 시 SQS가 자동으로 메시지를 재전달하는 방식으로 복구한다.

### 핵심 변경사항

1. **메시지 삭제 시점 변경**: `finally` 블록(항상 삭제)에서 성공 시에만 삭제로 변경. 처리 실패 또는 태스크 크래시 시 메시지가 삭제되지 않아 visibility timeout 후 SQS에 다시 나타난다.

2. **멱등성 키 기록 시점 변경**: 세션 생성 시점이 아닌 **RCA 완료 시점**에 멱등성 키를 기록한다. 이를 통해 재전달된 메시지가 중복 체크에 걸리지 않고 재처리된다.

3. **Heartbeat/Scan/Claim 제거**: DynamoDB 기반 복구 인프라(heartbeat 스레드, stale 세션 스캔, 조건부 클레임)를 모두 제거한다. SQS가 복구 메커니즘을 대체한다.

### 복구 흐름

```mermaid
flowchart TD
    RECV["SQS receive_message"] --> PROC["_process_message()"]
    PROC -->|성공| DEL["delete_message\n+ 멱등성 키 기록"]
    PROC -->|실패/예외| KEEP["메시지 삭제 안 함"]
    KEEP --> VT["Visibility Timeout 만료"]
    VT --> RECV
    
    CRASH["태스크 크래시"] --> VT2["Visibility Timeout 만료"]
    VT2 --> RECV2["다른 태스크가 메시지 수신"]
```

### SQS Visibility Timeout 설정

- SQS 큐의 Visibility Timeout을 CC Headless의 최대 처리 시간(`CC_TIMEOUT_SECONDS`, 기본 600초)보다 충분히 크게 설정한다 (예: 900초).
- 처리 중 다른 컨슈머가 같은 메시지를 받지 않도록 보장한다.

### Graceful Shutdown on SIGTERM

ECS 롤링 배포나 수동 중지 시 SIGTERM이 도착하면 에이전트는 단순히 루프를 탈출하는 것만으로 충분하지 않다. 진행 중인 RCA 세션이 `ANALYZING` 등 비종료 상태로 DDB에 남으면 대시보드에서 "분석중"으로 영구 고착되고, 동일 알람이 재전달되어도 멱등성 체크에 걸려 재시도되지 않는다.

따라서 SIGTERM 수신 시 다음 규칙을 따른다:

1. **Shutdown 신호 전파**: 시그널 핸들러는 공유 이벤트만 set한다 (DDB/네트워크 호출 금지 — 시그널 안전성 확보).
2. **긴 블로킹 작업 중단**: 에이전트 실행(CC subprocess, Bedrock agent 호출 등) 중에는 주기적으로 shutdown 이벤트를 감지할 수 있는 체크 지점을 둔다. 체크 지점은 파이프라인의 주요 단계 사이와 반복 루프 내(예: 가설별 증거 수집 사이)에 배치한다.
3. **세션 마킹 1회**: 중단이 확인되면 해당 세션을 한 번만 `FAILED`로 전이하고, 에이전트를 정상 종료시킨다. 재시도 루프는 두지 않는다.
4. **ECS stopTimeout 여유 확보**: Bedrock 호출 등 단일 체크 지점 사이의 블로킹이 30초를 초과할 수 있으므로, ECS 컨테이너의 `stopTimeout`을 120초로 설정하여 SIGKILL 전에 graceful 경로가 완결될 시간을 확보한다.

이 경로가 동작한 뒤에는 SQS 재전달 메커니즘(상단의 복구 흐름)과 결합되어, 재전달된 메시지는 기존 `FAILED` 세션과 다른 새 세션으로 정상 재처리된다.

## Consequences

### Positive

- **단순성**: heartbeat 스레드, stale 스캔, 조건부 클레임 등 복잡한 복구 로직이 불필요하다
- **신뢰성**: SQS의 검증된 재전달 메커니즘에 의존하므로 false positive(정상 세션을 stale로 오판)이 없다
- **CPU 쓰로틀 내성**: heartbeat 방식과 달리 CPU 부하가 높아도 복구 판단에 영향을 주지 않는다
- **다중 인스턴스 안전**: SQS가 메시지를 하나의 컨슈머에게만 전달하므로 경합이 발생하지 않는다

### Negative

- **재처리 지연**: visibility timeout이 만료될 때까지 기다려야 하므로 크래시 후 복구까지 최대 visibility timeout만큼 지연된다
- **처음부터 재실행**: 중간 상태를 복원할 수 없으므로 이미 수행한 분석이 반복된다 (CC Headless는 subprocess 기반이라 중간 상태 직렬화 불가)
- **DDB에 FAILED 세션 누적**: 크래시 후 재처리 시 이전 세션이 FAILED로 남고 새 세션이 생성된다

### Risks

- Visibility timeout이 실제 처리 시간보다 짧으면 처리 중에 메시지가 재전달되어 중복 처리가 발생할 수 있다. `CC_TIMEOUT_SECONDS`보다 50% 이상 여유를 두고 설정한다.

## Related

- [ADR infra/0001: 알람 수신 아키텍처](0001-alarm-ingestion-sns-sqs-fargate.md) — SQS 메시지 처리 흐름
- [ADR infra/0003: CC Headless 스택](0003-lambda-cc-headless-stack.md) — CC Headless ECS Fargate 인프라
- [ADR infra/0005: 실행 트레이스 DynamoDB](0005-execution-trace-dynamodb.md) — 세션 상태 관리
