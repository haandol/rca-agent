# RCA 및 Remediation High 발견사항 추적

## 문서 목적

2026-07-21에 수행한 Strands RCA, CC Headless RCA, 자동 복구, Healthcare 장애
주입/리셋, 인프라, 대시보드 전체 흐름 점검에서 확인된 High 심각도 문제를
추적한다. 새 작업 세션은 이 문서를 기준으로 미해결 항목을 선택하고, 수정과
회귀 검증이 끝날 때까지 상태와 근거를 갱신한다.

- 점검 기준 커밋: `0b2e279`
- 최초 작성일: 2026-07-21
- 전체 상태: `OPEN`
- 범위: `packages/agent`, `packages/cc-headless`, `packages/healthcare-sensor-app`,
  `packages/infra`, `packages/dashboard`

이 문서는 아키텍처 결정을 대신하는 ADR이 아니다. 수정 과정에서 기존 결정이
바뀌면 관련 ADR을 먼저 갱신하고, 단순 버그 수정이면 이 추적 문서와 테스트만
갱신한다.

## 새 세션 시작 절차

1. 루트 `AGENTS.md`, 대상 패키지의 `AGENTS.md`, 관련 ADR을 읽는다.
2. 아래 표에서 `OPEN` 항목 하나를 선택해 `IN_PROGRESS`로 변경한다.
3. 문제를 현재 `main`에서 다시 재현하고 재현 테스트를 먼저 추가한다.
4. 수정 후 항목별 완료 조건과 공통 검증을 모두 실행한다.
5. 코드만 수정된 상태는 `FIXED`, 회귀 테스트와 통합 검증까지 통과한 상태는
   `VERIFIED`로 기록한다.
6. 상태, 검증 일자, 커밋 또는 PR, 남은 위험을 이 문서에 갱신한다.

상태 값은 `OPEN`, `IN_PROGRESS`, `FIXED`, `VERIFIED`, `DEFERRED`만 사용한다.

## 우선순위

| 순서 | 묶음 | 항목 |
|---|---|---|
| 1 | 자동 복구 안전 경계 | H-03, H-04, H-08, H-11, H-12 |
| 2 | 세션 소유권과 재처리 | H-01, H-02, H-07, H-17, H-18 |
| 3 | 실제 장애 해제 보장 | H-13, H-14, H-15, H-16 |
| 4 | RCA 완료 정확성 | H-05, H-06, H-09, H-10 |
| 5 | 외부 입력과 로컬 운영 보안 | H-19, H-20 |

## 현황 요약

| ID | 영역 | 요약 | 상태 | 담당/PR |
|---|---|---|---|---|
| H-01 | Strands | 실패·중단 세션 재전달이 RCA를 재실행하지 못함 | VERIFIED | `e4e0776` |
| H-02 | Strands | 상태 전이가 claim으로 fencing되지 않음 | VERIFIED | `e4e0776` |
| H-03 | Strands Remediation | 복구 액션 유형이 검증 증거가 아닌 초기 모델 분류에 의존 | VERIFIED | `e4e0776` |
| H-04 | Strands Remediation | LLM boolean을 정상화의 권위 있는 판정으로 사용 | VERIFIED | `e4e0776` |
| H-05 | Strands | 선언된 LLM timeout이 실제 실행 시간을 제한하지 못함 | VERIFIED | `e4e0776` |
| H-06 | Strands | accepted-review grace 경로가 확정 가설을 미확정으로 보고 가능 | VERIFIED | `e4e0776` |
| H-07 | Strands Remediation | 복구 완료 저장 전에 결과 이벤트를 발행 | VERIFIED | `e4e0776` |
| H-08 | CC Remediation | 복구가 서버 소유 알람 대상과 바인딩되지 않음 | VERIFIED | `e4e0776` |
| H-09 | CC | 필수 플레이북 저장 실패 후에도 세션 완료·메시지 삭제 | VERIFIED | `e4e0776` |
| H-10 | CC | 분기 가설을 후속 validation loop에서 확정할 수 없음 | VERIFIED | `e4e0776` |
| H-11 | CC Remediation | CloudWatch M-of-N 조건을 무시해 조기 NORMALIZED 가능 | VERIFIED | `25570cd` |
| H-12 | CC Remediation | 경쟁 가설이 미해결이어도 자동 복구를 허용 | VERIFIED | `597eafd` |
| H-13 | Healthcare | DB leak 주입과 reset 경쟁 시 reset 후 연결이 남음 | VERIFIED | `8c97524` |
| H-14 | Healthcare | fault가 남아도 reset API가 성공을 반환 가능 | VERIFIED | `573688b` |
| H-15 | Healthcare | slow-query가 다른 event loop의 AsyncEngine을 사용하고 오류를 숨김 | VERIFIED | `7b513a6`, `a614036` |
| H-16 | Healthcare/Infra | 일부 fault가 알람을 발생시키거나 정상화를 검증할 수 없음 | IN_PROGRESS | 로컬 검토 중 |
| H-17 | Dashboard | 활성 세션 삭제가 claim/lease fencing을 제거 | OPEN | - |
| H-18 | Dashboard | 취소가 현재 claim과 late trace write를 fence하지 않음 | OPEN | - |
| H-19 | Dashboard | 모델·S3 Markdown을 sanitizing 없이 HTML로 렌더링 | OPEN | - |
| H-20 | Infra | CloudWatch SNS publish 정책에 source 제한이 없음 | OPEN | - |

## 상세 발견사항

### H-01 Strands 실패 세션 재처리 불가

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: SQS 메시지 식별자와 receive count를 claim 세대로 사용하고, 실패·진행
  세션의 원자적 reclaim, claim 기반 상태·trace fencing, 시도별 보고서 격리,
  evidence/playbook 저장 lease를 적용했다.
- **검증**:
  - 실제 DynamoDB 저장소와 오케스트레이터를 연결한
    `FAILED → 재전달 → COMPLETED` 통합 테스트 통과
  - Agent tests: 429 passed, 4 xfailed
  - `pnpm verify`, infra build, dashboard build 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: expected state 원자 검증은 H-02, 알림 outbox는 H-07에서
  계속 추적한다.
- **영향**: 일시적인 LLM, AWS API, 저장소 오류로 `FAILED`가 된 RCA는 SQS가
  재전달해도 다시 실행되지 않고 최대 수신 횟수 이후 DLQ로 이동한다.
- **원인**: 소비자가 `ApproximateReceiveCount`를 읽지 않으며, 기존 세션은 상태와
  무관하게 중복으로 처리된다. 완료 handoff도 `COMPLETED` 외 상태를 거부한다.
- **근거**:
  - `packages/agent/src/rca_agent/adapters/secondary/queue/sqs_consumer.py:20`
  - `packages/agent/src/rca_agent/services/pipeline.py:154`
  - `packages/agent/src/rca_agent/services/pipeline.py:217`
  - `packages/agent/src/rca_agent/adapters/secondary/session/dynamodb_session_store.py:157`
- **완료 조건**: 더 큰 receive count만 실패·중단 세션을 원자적으로 reclaim하고,
  이전 실행은 상태·산출물·알림을 확정할 수 없어야 한다. 재전달 통합 테스트가
  최초 실패 후 두 번째 수신의 완료를 검증해야 한다.

### H-02 Strands 상태 전이의 claim fencing 부재

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: 상태 전이 전 일관 읽기로 확인한 정확한 source state와 현재 claim token을
  하나의 DynamoDB 조건부 쓰기에서 검증하도록 통합했다. 조건 경쟁은 파이프라인을
  fail-closed로 중단하고, 저장소 오류는 소유권 확인 실패로 구분한다.
- **검증**:
  - 실제 DynamoDB 저장소에서 조회 직후 더 큰 receive count가 동일 메시지를
    reclaim하는 경쟁을 주입
  - 이전 claim의 SCOPING, FAILED, COMPLETED 쓰기 차단 및 새 claim 상태 보존 확인
  - Agent tests: 439 passed, 4 xfailed
  - `pnpm verify`, Ruff, `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: 동일 claim에서 상태가 변경된 뒤 같은 source state로 되돌아오는
  ABA 경쟁은 별도 transition version 없이는 탐지할 수 없다. 현재 상태 머신은
  terminal 상태의 역전이를 금지하고 단일 claim 파이프라인을 전제로 한다.
- **영향**: 이전 실행이나 동시 실행이 최신 상태를 덮어쓰거나 terminal 상태를
  non-terminal 상태로 되돌릴 수 있다.
- **원인**: 현재 상태 조회와 갱신이 분리되어 있고 조건식이 현재 claim과 정확한
  이전 상태를 함께 검증하지 않는다. 완료 갱신 조건은 더 약하다.
- **근거**:
  - `packages/agent/src/rca_agent/adapters/secondary/session/dynamodb_session_store.py:93`
  - `packages/agent/src/rca_agent/adapters/secondary/session/dynamodb_session_store.py:103`
  - `packages/agent/src/rca_agent/adapters/secondary/session/dynamodb_session_store.py:217`
- **완료 조건**: 모든 상태 전이와 완료 기록이 expected state와 claim token을
  하나의 조건부 쓰기에서 검증해야 한다. interleaving 테스트가 이전 claim의
  SCOPING/FAILED/COMPLETED 쓰기를 모두 차단해야 한다.

### H-03 Strands 복구 액션의 증거 독립 검증 부재

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: 초기 가설의 `fault_type`은 조사 힌트로만 유지하고, validation 단계가
  실제 증거와 증거 요약을 바탕으로 독립 산출한 `validated_fault_type`만 세션,
  완료 알림, 복구 액션에 전달하도록 변경했다. 확정 상태, 증거, 검증 유형 누락
  또는 세션·선택 가설 유형 불일치는 remediation claim 전에 차단한다.
- **검증**:
  - 초기 DB leak 제안과 검증된 HIGH_CPU 유형을 실제 DynamoDB에 함께 저장한 뒤,
    세션 재조회와 복구 오케스트레이터가 HIGH_CPU만 실행하는 통합 테스트 통과
  - 증거 수집 실패, 빈 증거/요약, 미확정 판정, legacy 필드 누락, 유형 불일치의
    `UNSUPPORTED` fail-closed 테스트 통과
  - Agent tests: 448 passed, 4 xfailed
  - `pnpm verify`, 오프라인 평가 6건, Ruff, `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: 실제 Bedrock 모델과 AWS 데이터로 수행하는 선택적 라이브 평가는
  실행하지 않았다.
- **영향**: 실제 CPU 원인 가설이 초기 생성 단계에서 DB leak로 잘못 라벨링되면,
  가설 설명이 확정되어도 DB reset이 실행될 수 있다.
- **원인**: `fault_type`은 증거 수집 전에 모델이 선택하고 validation 결과는 이를
  다시 판정하지 않는다. 복구 gate는 세션과 hypothesis에 복사된 동일 값을 비교한다.
- **근거**:
  - `packages/agent/src/rca_agent/services/hypothesis.py:51`
  - `packages/agent/src/rca_agent/services/validation.py:28`
  - `packages/agent/src/rca_agent/services/pipeline.py:910`
  - `packages/agent/src/rca_agent/services/remediation.py:50`
- **완료 조건**: 서버 소유 검증 결과가 증거와 구조화된 원인 유형을 함께 확정하고,
  설명·증거·액션 유형 불일치는 fail-closed 처리해야 한다.

### H-04 Strands 정상화 판정이 LLM 출력에 의존

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: 서버가 세션 소유 alarm name/region으로 CloudWatch 알람 정의를 다시
  조회하고, 원본 statistic, unit, period, M-of-N, threshold, comparison operator를
  이용해 `NORMALIZED`, `FAILED`, `PENDING`을 계산하도록 변경했다. 복구 이후 N개
  완전 period와 ingestion grace를 기다리되 remediation claim보다 짧게 제한하고,
  API/클라이언트 오류, 누락 datapoint, 미지원 알람, 저표본 percentile ignore는
  `PENDING`으로 fail-closed 처리한다. LLM은 선택적 설명만 추가하며 상태를
  변경하지 못한다.
- **검증**:
  - M-of-N 정상/위반/부족, strict comparison, standard/extended statistic,
    unit, 사전·겹침 period 제외, session alarm/region binding 회귀 테스트 통과
  - raw notification 알람 덮어쓰기, LLM 상태 변경, LLM·CloudWatch client 초기화
    실패, 구조화 알림/저장 상태 전파 테스트 통과
  - Agent tests: 489 passed, 4 xfailed
  - `pnpm verify`, 오프라인 평가 6건, Ruff, `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: 실제 CloudWatch metric ingestion 지연과 Bedrock 모델을 포함한
  선택적 라이브 검증은 실행하지 않았다.
- **영향**: CloudWatch 조회 실패, 도구 미호출, datapoint 부족 상황에서도 모델이
  `metrics_normalized=true`를 반환하면 복구 성공으로 저장될 수 있다.
- **원인**: 정상화 boolean을 코드가 다시 계산하지 않으며 실패와 관측 대기를
  구분하지 못한다.
- **근거**:
  - `packages/agent/src/rca_agent/services/verification.py:84`
  - `packages/agent/src/rca_agent/adapters/secondary/session/dynamodb_session_store.py:445`
- **완료 조건**: 서버 코드가 원래 알람 정의와 datapoint를 이용해
  `NORMALIZED`, `FAILED`, `PENDING`을 판정해야 한다. LLM은 설명 생성에만
  사용하고 권위 있는 상태를 변경하지 않아야 한다.

### H-05 Strands timeout이 wall-clock을 제한하지 못함

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: executor context 기반 timeout을 제거하고 Linux/POSIX 동기 CLI의 메인
  스레드에서 `SIGALRM` hard timer로 실행 중 호출 스택 자체를 중단하도록 통일했다.
  invocation별 `BaseException` token으로 일반 `except Exception`이 timeout을
  삼키지 못하게 하고, 활성 process timer나 비-POSIX/worker thread에서는 작업을
  시작하지 않고 fail-closed 처리한다. Evidence와 playbook의 반복 호출은 단계
  전체 monotonic deadline을 공유한다.
- **검증**:
  - scoping, evidence, prioritization, report, playbook update/generation,
    verification summary의 양수 timeout wall-clock 회귀 테스트 통과
  - timeout 후 late mutation 없음, operation 미시작, 일반 예외 재전파,
    signal handler 복구, 기존 timer 보존, 다중 호출 누적 deadline 테스트 통과
  - Agent tests: 505 passed, 4 xfailed
  - `pnpm verify`, 오프라인 평가 6건, Ruff, `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: hard timeout은 배포 환경인 POSIX 메인 스레드를 전제로 한다.
  그 외 실행 환경은 제한 없는 background 실행 대신 명시적으로 실패한다.
- **영향**: 시간 예산, SQS visibility timeout, remediation claim 시간을 초과해
  중복 실행과 소유권 만료가 발생할 수 있다.
- **원인**: future timeout 후 executor context를 빠져나올 때 실행 중인 thread를
  다시 기다린다.
- **근거**:
  - `packages/agent/src/rca_agent/services/scoping.py:152`
  - `packages/agent/src/rca_agent/services/evidence.py:171`
  - 동일 패턴: prioritization, report, playbook, verification
- **재현 결과**: 1초 timeout으로 설정한 호출이 5초 후 반환했다.
- **완료 조건**: 각 단계의 호출 제한이 실제 wall-clock 상한을 지켜야 하며,
  timeout 회귀 테스트가 허용 오차 내 elapsed time을 검증해야 한다.

### H-06 accepted-review grace 결과가 미확정으로 보고됨

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: review gate가 최신 판정 confidence 기준으로 accepted hypothesis ID를
  명시하고, grace 소진 early exit를 해당 `CONFIRMED` 가설의
  `TerminationReason.CONFIRMED` 결정으로 연결했다. 선택 ID가 현재 confirmed
  가설과 일치하지 않으면 확정하지 않고 검증 루프를 계속한다.
- **검증**:
  - confidence 0.85 accepted 가설의 expansion-blocked grace 전체 orchestration
    테스트에서 최종 report, notification, session이 동일 가설과
    `confirmed=true`를 기록함을 확인
  - 최신 judgment 선택, accepted 부재 fail-closed, 기존 0.9 이상 확정과
    time-budget 종료 회귀 테스트 통과
  - Agent tests: 507 passed, 4 xfailed
  - `pnpm verify`, 오프라인 평가 6건, Ruff, `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: 없음
- **영향**: review gate에서 수용된 고신뢰 가설이 최종 보고서와 복구 gate에서는
  미확정으로 처리될 수 있다.
- **원인**: beam selection은 `CONFIRMED`를 제외하고, child가 없으면 루프가
  종료되지만 최종 확정 boolean은 특정 termination reason에서만 설정된다.
- **근거**:
  - `packages/agent/src/rca_agent/services/pipeline.py:79`
  - `packages/agent/src/rca_agent/services/pipeline.py:782`
  - `packages/agent/src/rca_agent/services/pipeline.py:835`
- **재현 결과**: confidence 0.85 accepted 가설이 best로 선택됐지만
  `confirmed=false`로 종료됐다.
- **완료 조건**: accepted 상태의 의미와 최종 confirmed 상태가 하나의 명시적
  계약을 따라야 하며 전체 orchestration 테스트가 이를 검증해야 한다.

### H-07 복구 결과 발행과 완료 저장 순서 오류

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: 복구 결과, 서버 검증 결과, 결정적 `publication_id`를 가진 알림
  payload와 `PENDING` 상태를 복구 claim 완료와 함께 원자 저장한 뒤에만 별도
  publication lease로 SNS 발행을 시작하도록 변경했다. 완료 중복은 reset과 검증을
  다시 실행하지 않고 저장된 payload만 발행하며, 경합·발행 실패·`SENT` 저장
  실패는 SQS ack를 차단한다. 복구 claim과 publication lease는 제한된 reset,
  CloudWatch 관측, LLM, AWS SDK, SNS 재시도의 최악 실행 시간보다 길게 계산한다.
- **검증**:
  - 실제 DynamoDB 저장소를 사용해 완료 저장 선행, `SENT` 저장 일시 실패,
    SNS 실패 후 재전달, 동시 publication 단일 승자, lease 만료 reclaim을 검증
  - reset/verification 각 1회, 저장된 동일 payload와 `publication_id` 사용,
    publication 경합 시 SQS ack 차단 확인
  - Agent tests: 516 passed, 4 xfailed
  - `pnpm verify`, Ruff, `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: 표준 SNS는 at-least-once이므로 publish 성공 직후 프로세스가
  종료되면 동일 payload가 재발행될 수 있다. 소비자는 결정적 `publication_id`를
  멱등성 키로 사용해야 한다.
- **영향**: SNS publish 성공 후 claim 완료 저장이 실패하면 재전달에서 reset,
  검증, 결과 알림이 중복 실행된다.
- **원인**: 외부 결과 이벤트를 먼저 발행하고 DynamoDB 완료를 나중에 기록한다.
- **근거**:
  - `packages/agent/src/rca_agent/services/remediation_pipeline.py:120`
  - `packages/agent/src/rca_agent/services/remediation_pipeline.py:129`
- **완료 조건**: 재시도 가능한 outbox 또는 멱등 publication handoff를 사용하고,
  publish 후 저장 실패를 주입한 테스트에서 reset과 결과 이벤트가 각각 한 번만
  관찰돼야 한다.

### H-08 CC 복구 대상이 알람과 바인딩되지 않음

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: 현재 claim의 DynamoDB 세션에서 원본 alarm data를 일관 읽기하고, lease
  획득 전에 fault별 namespace, metric, dimensions를 배포 시 주입한 Healthcare
  ECS cluster/service 또는 RDS instance 식별자와 정확히 비교한다. DB leak은
  `DatabaseConnections`, slow query는 `ReadLatency`, CPU/Memory는 각 ECS
  utilization metric만 허용하며 누락·불일치는 `BLOCKED`로 종료한다.
- **검증**:
  - 비-Healthcare namespace, 같은 metric의 다른 리소스, fault/metric 불일치,
    alarm data/기대 리소스 설정 누락에서 lease와 HTTP 모두 미호출 확인
  - stale claim 차단과 네 가지 허용 fault의 정확한 target 성공 경로 확인
  - CC tests: 190 passed
  - Infra 대상 tests: 5 passed, infra build 통과
  - 계약 테스트 16건, 오프라인 평가 6건, Ruff, typecheck, `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: slow-query `ReadLatency` 알람 자체의 배포 계약은 H-16에서 계속
  추적한다. 해당 알람이 없으면 자동 복구가 시작되지 않는다.
- **영향**: Healthcare와 무관한 알람도 모델이 허용 fault로 분류하면 Healthcare
  reset을 실행할 수 있다.
- **원인**: 복구 gate는 모델 작성 산출물을 검증하지만 서버 소유 alarm namespace,
  dimensions, resource identity는 reset 전에 허용 목록과 비교하지 않는다.
- **근거**:
  - `packages/cc-headless/src/cc_headless/mcp_server.py:221`
  - `packages/cc-headless/src/cc_headless/mcp_server.py:317`
  - alarm data는 reset 이후 verification 단계인 `mcp_server.py:343`에서 읽는다.
- **완료 조건**: lease 획득 전에 서버 소유 알람이 허용된 Healthcare 리소스,
  namespace, metric, dimensions와 일치해야 한다. 비-Healthcare 알람 테스트에서
  HTTP 요청과 lease 획득이 모두 없어야 한다.

### H-09 CC 필수 플레이북 저장 실패 무시

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: 필수 playbook의 S3 Vectors 저장이 명시적으로 `True`를 반환해야만
  보고서 저장, 알림, 세션 완료로 진행하도록 completion gate를 강화했다. `False`
  또는 예외는 최종 publication lease를 해제하고 세션을 실패로 기록하며
  `process_message=False`를 반환해 SQS 메시지를 확인하지 않는다.
- **검증**:
  - 저장소 `False`와 예외 각각에서 report/notification/mark_completed 미호출,
    lease 해제, mark_failed, 미ack 결과 확인
  - CC tests: 192 passed
  - `pnpm verify`, 계약 테스트 16건, 오프라인 평가 6건, Ruff,
    `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: 없음
- **영향**: ADR상 필수인 플레이북이 저장되지 않았는데도 세션이 `COMPLETED`가 되고
  SQS 메시지가 삭제된다.
- **원인**: S3 Vectors 저장 함수의 `False` 반환을 무시하고 성공 로그를 남긴다.
- **근거**:
  - `packages/cc-headless/src/cc_headless/services/pipeline.py:245`
  - `packages/cc-headless/src/cc_headless/services/pipeline.py:262`
  - `packages/cc-headless/src/cc_headless/adapters/secondary/playbook/s3_vectors_playbook_store.py:31`
- **완료 조건**: 필수 저장 실패는 완료 gate를 통과하지 못하고 메시지를 ack하지
  않아야 한다. 저장소 `False`와 예외를 각각 주입하는 테스트가 필요하다.

### H-10 CC 분기 가설의 후속 검증 불가

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: `hypotheses.json`과 `validation-1..N.json`을 숫자 순서로 검증하면서
  각 loop의 `new_hypotheses`를 누적한 뒤 후속 loop의 판정 대상을 검증하도록
  변경했다. 새 가설의 필수 필드, 전역 ID 유일성, 기존 parent, 동일 tree,
  `parent.depth + 1`을 강제하고 watcher도 같은 순서와 validator를 사용해 잘못된
  validation이 trace/DynamoDB 가설 상태를 갱신하지 못하게 했다.
- **검증**:
  - 첫 loop child를 두 번째 loop에서 확정하는 재현 테스트 통과
  - duplicate ID, 미상·미래 parent, 잘못된 tree/depth/필드 부정 테스트 통과
  - validation 파일 `2`, `10`의 숫자 순서 처리와 invalid DDB 반영 차단 확인
  - CC tests: 211 passed
  - `pnpm verify`, 계약 테스트 16건, 오프라인 평가 6건, Ruff,
    `git diff --check` 통과
- **커밋/PR**: `e4e0776`
- **남은 위험**: 없음
- **영향**: 첫 validation loop에서 생성한 child hypothesis를 다음 loop에서
  확정하면 완료 검증이 실패한다.
- **원인**: validator의 알려진 hypothesis 집합이 최초 `hypotheses.json`에만
  기반하며 이전 loop의 `new_hypotheses`를 누적하지 않는다.
- **근거**:
  - `packages/cc-headless/src/cc_headless/services/artifact_validation.py:143`
  - `packages/cc-headless/src/cc_headless/services/artifact_validation.py:183`
  - `packages/cc-headless/src/cc_headless/services/artifact_watcher.py:315`
- **재현 결과**: 두 번째 loop가 child를 확정하면
  `validation-2.json references an unknown hypothesis`가 발생했다.
- **완료 조건**: validation loop 순서대로 가설 트리를 누적하고 parent/depth/ID
  정합성을 검증해야 한다.

### H-11 CC CloudWatch M-of-N 판정 누락

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: SNS 알람 payload에서 evaluation periods, datapoints-to-alarm,
  missing-data 정책을 보존하고, reset 이후 완전히 종료된 최신 N개 period만
  원본 M-of-N 조건으로 평가한다. 누락 정책이 `breaching` 또는
  `notBreaching`일 때만 누락 period를 채우며, 관측 구간 부족과 확정할 수 없는
  정책은 `PENDING`으로 처리한다.
- **검증**:
  - `EvaluationPeriods=2`에서 정상 datapoint 하나만 관측된 회귀 테스트 통과
  - M-of-N 정상화/실패, 누락 정책, partial·pre-reset·범위 밖 datapoint,
    중복·정렬, strict threshold, 잘못된 M/N 테스트 통과
  - CC Headless tests: 232 passed
  - Ruff lint/format 및 `git diff --check` 통과
- **커밋/PR**: `25570cd`
- **남은 위험**: CloudWatch metric 수집 지연 또는 `ignore`/`missing` 정책에서는
  제한된 재시도 안에 판정을 확정하지 못해 후속 관측이 필요할 수 있다.
- **영향**: 실제 알람이 아직 OK 전환 조건을 충족하지 않았는데 복구 결과를
  `NORMALIZED`로 보고할 수 있다.
- **원인**: `EvaluationPeriods`와 `DatapointsToAlarm`을 보존하지 않고 하나의
  non-breaching datapoint를 정상화로 처리한다.
- **근거**:
  - `packages/cc-headless/src/cc_headless/ports/dto/models.py:20`
  - `packages/cc-headless/src/cc_headless/services/post_reset_verification.py:126`
  - `packages/infra/lib/stacks/healthcare-service-stack.ts:201`
- **재현 결과**: `EvaluationPeriods=2`인 알람에서 정상 datapoint 하나만으로
  `NORMALIZED`가 반환됐다.
- **완료 조건**: 원본 알람의 period, evaluation periods, datapoints-to-alarm,
  missing-data 정책을 반영한 M-of-N 판정 테스트가 통과해야 한다.

### H-12 CC 경쟁 가설 미해결 상태에서 복구 허용

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: 모든 validation loop의 가설별 최신 분류를 누적하고, 확정 fault와
  다른 allowlisted reset으로 이어지는 가설이 미분류, 조사 중 또는 해소되지 않은
  과거 confirmed 상태이면 복구를 차단한다. 경쟁 가설이 후속 loop에서
  `rejected`/`closed`로 해소되거나 같은 reset action으로 수렴하는 경우만 허용한다.
- **검증**:
  - 단일 확정과 동일 fault 복수 확정 허용 테스트 통과
  - 다른 fault의 미분류·조사 중·과거 confirmed 차단 테스트 통과
  - 후속 `rejected`/`closed` 해소와 unsupported 경쟁 가설 허용 테스트 통과
  - MCP 통합 테스트에서 reset lease와 HTTP 호출 전 `BLOCKED` 확인
  - CC Headless tests: 246 passed
  - Ruff lint/format 및 `git diff --check` 통과
- **커밋/PR**: `597eafd`
- **남은 위험**: 가설의 fault type과 terminal 분류 자체는 모델 산출물이므로 서버는
  구조·상태 정합을 검증하지만 증거의 의미를 독립 재판정하지는 않는다. H-08의
  서버 소유 알람 대상·metric 바인딩이 잘못된 reset의 추가 안전 경계로 남는다.
- **영향**: high-cpu가 confirmed여도 db-leak 경쟁 가설이 investigation 상태면
  잘못된 reset을 실행할 수 있다.
- **원인**: 복구 증거 validator가 모든 경쟁 가설의 terminal 상태를 요구하지 않는다.
- **근거**:
  - `packages/cc-headless/src/cc_headless/services/artifact_validation.py:171`
  - `packages/cc-headless/src/cc_headless/services/artifact_validation.py:299`
- **완료 조건**: 상충하는 allowlisted 원인이 남아 있으면 `BLOCKED`로 종료해야 한다.
  단일 확정, 복수 확정, 미해결 경쟁 원인 테스트를 각각 추가한다.

### H-13 DB leak 주입/reset 경쟁 조건

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: explicit DB leak 요청을 generation과 in-flight acquisition으로
  추적한다. reset은 이전 generation을 fence하고 기존 연결을 먼저 닫은 뒤 진행 중
  획득이 끝날 때까지 기다려 late connection도 닫는다. reset 중 시작한 새 injection은
  reset 완료 후 새 generation에서 실행한다.
- **검증**:
  - connect 완료 전 reset이 시작되는 결정론적 경쟁 테스트 통과
  - reset 중 신규 injection 직렬화 테스트 통과
  - injection cancellation 시 reset waiter와 acquisition count 정리 테스트 통과
  - Healthcare tests: 19 passed, 1 xfailed
  - Ruff lint/format 및 `git diff --check` 통과
- **커밋/PR**: `8c97524`
- **남은 위험**: PostgreSQL 연결 획득 자체가 무기한 반환되지 않으면 reset도 해당
  in-flight 요청을 기다린다. 운영 HTTP/DB timeout이 이 대기 시간의 외부 상한이다.
- **영향**: reset API가 성공한 뒤에도 leak connection이 남아 장애가 지속될 수 있다.
- **원인**: connection 획득 후 전역 목록 추가 전에 await 지점이 있고 reset은
  동기화 없이 목록을 snapshot/clear한다.
- **근거**:
  - `packages/healthcare-sensor-app/src/test_service/services/fault.py:134`
  - `packages/healthcare-sensor-app/src/test_service/services/fault.py:159`
- **재현 결과**: reset은 `closed=0`을 반환했지만 완료 직후 leaked connection이
  1개 존재했다.
- **완료 조건**: injection과 reset을 직렬화하거나 generation fencing을 적용하고,
  경쟁 테스트가 reset 응답 후 leak count 0을 보장해야 한다.

### H-14 fault가 남아도 reset 성공 응답 가능

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: CPU reset은 join 후 생존 thread를 확인해 `stop_timeout`과 잔존 수를
  반환하고 참조를 유지한다. Healthcare controller는 `failed`/`stop_timeout`을
  HTTP 500으로 전달한다. Strands는 CC와 동일하게 bounded JSON object 응답과
  `stopped`/`not_running` status만 성공으로 인정한다.
- **검증**:
  - CPU thread 잔존 시 HTTP 500, 참조 유지, 후속 reset 성공 테스트 통과
  - slow-query `stop_timeout` HTTP 500 테스트 통과
  - Strands invalid/oversized/non-success 응답 실패 판정 테스트 통과
  - CC non-success/invalid body 계약 회귀 테스트 통과
  - Healthcare tests: 20 passed, 1 xfailed
  - Agent tests: 530 passed, 4 xfailed
- **커밋/PR**: `573688b`
- **남은 위험**: timeout 이후 살아 있는 daemon thread는 프로세스 안에 남으므로
  후속 reset 재시도 또는 태스크 교체가 필요하다. 성공으로 오인되지는 않는다.
- **영향**: 복구 엔진과 최종 보고서가 실제 장애 지속 상태를 성공으로 기록한다.
- **원인**: CPU reset은 join timeout 후 thread 생존 여부를 확인하지 않는다.
  slow-query는 `stop_timeout` 결과도 HTTP 200으로 반환하며 Strands는 모든 2xx를
  성공으로 간주한다.
- **근거**:
  - `packages/healthcare-sensor-app/src/test_service/services/fault.py:224`
  - `packages/healthcare-sensor-app/src/test_service/services/fault.py:289`
  - `packages/healthcare-sensor-app/src/test_service/adapters/primary/fault/fault_controller.py:30`
  - `packages/agent/src/rca_agent/services/remediation.py:22`
- **완료 조건**: reset 응답은 실제 종료를 확인해야 하며 timeout/잔존 fault는
  비성공 상태와 적절한 HTTP 오류로 전달돼야 한다. 두 엔진이 응답 body와 status를
  동일하게 해석해야 한다.

### H-15 slow-query의 cross-event-loop AsyncEngine 사용

- **상태**: `VERIFIED` (2026-07-21)
- **수정**: slow-query worker thread가 자체 event loop에서 전용 asyncpg connection을
  생성·사용·종료하도록 분리했다. 쿼리는 parameter binding을 사용하고, 연결·쿼리
  실패를 로깅한 뒤 최대 2초 bounded backoff로 재시도한다.
- **검증**:
  - worker thread와 단일 전용 event loop에서 connect/execute/close 실행 확인
  - 애플리케이션 `DatabasePort.session()` 미호출 확인
  - `SELECT pg_sleep($1)` parameter binding과 오류 backoff 테스트 통과
  - PostgreSQL 16에서 실제 `pg_sleep`를 `pg_stat_activity`로 관측하고 reset 후
    worker connection 종료 확인: 1 passed
  - Healthcare tests: 23 passed, 1 skipped, 1 xfailed
- **커밋/PR**: `7b513a6`, `a614036`
- **남은 위험**: 실행 중인 `pg_sleep`는 즉시 취소하지 않으므로 stop 응답 시간은
  현재 쿼리 interval과 join timeout의 영향을 받는다.
- **영향**: 의도한 DB 부하 대신 loop-bound asyncpg 오류가 반복되거나, 예외를
  숨긴 CPU spin이 발생할 수 있다.
- **원인**: 별도 thread event loop에서 애플리케이션 메인 loop의 pooled
  `AsyncEngine`을 사용하고 모든 예외를 무시한다.
- **근거**:
  - `packages/healthcare-sensor-app/src/test_service/services/fault.py:31`
  - `packages/healthcare-sensor-app/src/test_service/services/fault.py:297`
- **완료 조건**: 하나의 event loop에서 관리되는 task 또는 thread 전용 동기
  connection을 사용하고, 실제 PostgreSQL 통합 테스트로 query 실행과 reset을
  검증해야 한다.

### H-16 fault와 CloudWatch 알람/검증 신호 불일치

- **상태**: `IN_PROGRESS` (2026-07-21)
- **검토 결과**:
  - high-cpu는 ECS `CPUUtilization` 알람과 주입·reset 계약이 일치한다.
  - explicit db-leak는 `count=35`일 때 실제 AWS에서 `DatabaseConnections`
    임계치 30을 넘겼지만 기본 count 10과 environment pool 최대 15는 부족하다.
  - high-memory는 ECS `MemoryUtilization`과 연결되지만 기본 할당량이 임계치 80%를
    안정적으로 넘는다는 라이브 근거가 없다.
  - slow-query의 `pg_sleep`는 RDS storage `ReadLatency`를 안정적으로 높이지
    않으며 현재 해당 알람도 없다.
  - 60초 period 2개를 요구하는 알람은 reset 시점 정렬을 포함하면 최대 약 180초가
    필요해 CC 기본 120초 관측 예산으로는 `NORMALIZED` 확정이 어렵다.
- **다음 구현 경계**: 네 allowlisted fault만 대상으로 주입량 계약, slow-query용
  권위 metric, CDK 알람, H-08 target mapping, H-11 관측 예산을 하나의 ADR-first
  변경으로 정렬한 뒤 동일 배포에서 fault별 라이브 흐름을 검증한다. 환경 변수 기반
  request latency fault는 별도 지원 범위로 분리한다.
- **영향**: 장애 주입이 RCA를 시작하지 못하거나 reset 후 정상화 여부를 판정할 수
  없어 전체 데모 흐름이 성립하지 않는다.
- **원인**: slow-query/request latency 알람이 없고, 환경 기반 DB leak의 최대
  SQLAlchemy pool 사용량은 RDS connection 알람 임계치보다 낮다.
- **근거**:
  - `packages/infra/lib/stacks/healthcare-service-stack.ts:189`
  - `packages/infra/lib/stacks/healthcare-service-stack.ts:200`
  - `packages/infra/test/healthcare-service-stack.test.ts:57`
  - `packages/healthcare-sensor-app/src/test_service/config/settings.py:39`
- **완료 조건**: 지원 fault마다 `inject → ALARM → RCA → reset → OK/PENDING`
  계약이 정의되고 라이브 또는 AWS 통합 테스트로 검증돼야 한다.

### H-17 Dashboard 삭제가 활성 fencing을 제거

- **영향**: active claim/lease가 있는 세션을 삭제하면 기존 실행은 이미 시작한
  reset을 계속할 수 있고, SQS 재전달은 세션이 없다고 판단해 새 실행을 시작한다.
- **원인**: 삭제 API가 상태, claim, side-effect lease, remediation claim을 확인하지
  않고 엔진 레코드를 제거한다.
- **근거**:
  - `packages/dashboard/app/pages/index.vue:562`
  - `packages/dashboard/server/api/sessions/[id].delete.ts:40`
  - `packages/cc-headless/src/cc_headless/mcp_server.py:317`
- **완료 조건**: terminal이며 active lease/claim이 없는 세션만 삭제할 수 있어야
  한다. 메시지 retention 기간 동안 재생성을 막는 tombstone 전략도 검토한다.

### H-18 Dashboard 취소의 claim/trace fencing 부재

- **영향**: 취소 응답 이후 late artifact가 가설과 trace를 다시 기록할 수 있고,
  이미 lease 안에 들어간 side effect가 완료될 수 있다.
- **원인**: 취소는 state만 변경하고 claim token을 회전하거나 trace write 조건에
  non-terminal state를 포함하지 않는다.
- **근거**:
  - `packages/dashboard/server/api/sessions/[id]/cancel.post.ts:120`
  - `packages/cc-headless/src/cc_headless/services/artifact_watcher.py:202`
- **완료 조건**: 취소가 claim을 원자적으로 fence하고 이후 상태/trace/final
  publication 쓰기가 실패해야 한다. active side effect의 취소 의미를 명시적으로
  정의하고 테스트한다.

### H-19 Dashboard 저장형 XSS

- **영향**: 모델 또는 S3 산출물의 악성 HTML이 대시보드 origin에서 실행되어 인증
  없는 cancel/delete API를 호출할 수 있다.
- **원인**: `marked`가 보존한 raw HTML을 sanitizing 없이 `v-html`로 렌더링한다.
- **근거**:
  - `packages/dashboard/app/pages/report/[id].vue:20`
  - `packages/dashboard/app/pages/trace/[id].vue:822`
  - `packages/dashboard/app/pages/playbook/[id].vue:10`
- **재현 결과**: `marked`가 `<img onerror=...>` 이벤트 handler를 그대로 보존했다.
- **완료 조건**: strict allowlist sanitizer 또는 raw HTML 비활성화를 적용하고,
  script/event-handler/javascript URL 회귀 테스트와 CSP를 추가한다.

### H-20 CloudWatch SNS publish source 제한 부재

- **영향**: 다른 AWS 계정의 CloudWatch 서비스 요청이 RCA 입력 토픽에 이벤트를
  발행해 두 Bedrock 기반 엔진을 실행할 수 있다.
- **원인**: SNS resource policy가 service principal만 허용하고
  `aws:SourceAccount`와 alarm `aws:SourceArn` 조건을 사용하지 않는다.
- **근거**:
  - `packages/infra/lib/constructs/alarm-topic.ts:30`
- **완료 조건**: 현재 계정과 기대 alarm ARN으로 publish를 제한하고, CDK assertion
  테스트가 두 조건의 존재를 검증해야 한다.

## 공통 검증 기준

각 항목의 대상 테스트와 함께 다음 명령을 실행한다.

```bash
pnpm verify
pnpm --filter infra build
pnpm --filter dashboard build
```

인프라 계약이나 실제 알람·네트워크 동작을 변경한 항목은 CDK synth와 대상 AWS
통합 테스트를 추가한다. 자동 복구 안전 경계 변경은 다음 부정 테스트를 반드시
포함한다.

- 비-Healthcare 알람은 reset을 실행하지 않는다.
- 미확정 또는 경쟁 원인이 남은 RCA는 reset을 실행하지 않는다.
- claim을 잃거나 취소된 실행은 reset, report, notification을 확정하지 않는다.
- datapoint 부족과 CloudWatch 조회 실패는 성공이 아니라 `PENDING`이다.
- reset 응답 후 실제 fault state가 남아 있으면 성공으로 기록하지 않는다.

## 최초 점검 검증 결과

2026-07-21 기준:

- `pnpm verify`: 통과
- unit tests: 606 passed, 5 xfailed
- contract tests: 16 passed
- offline evaluation: 6 engine/scenario results passed
- lint, format check, typecheck: 통과
- dashboard build: 통과
- 10개 CDK stack synth: 통과

현재 테스트가 모두 통과하더라도 위 High 항목은 해결된 것으로 간주하지 않는다.
각 항목의 실패 재현 테스트와 완료 조건이 추가되고 통과해야 `VERIFIED`로 변경한다.
