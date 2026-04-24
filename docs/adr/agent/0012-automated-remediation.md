# ADR 0012: 자동 복구(Remediation) — 별도 에이전트로 분리한 이벤트 기반 복구

Date: 2026-04-23
Updated: 2026-04-24

## Status

Accepted

## Context

기존 RCA 에이전트(agent/0001~0009)는 분석 전용 시스템으로, 근본 원인을 파악하고 보고서와 플레이북을 생성하면 파이프라인이 종료된다. 실제 복구는 SRE가 보고서를 읽고 수동으로 수행해야 한다.

이 구조의 한계:

1. **MTTR 병목**: 분석은 자동화되었지만, 복구 실행까지의 지연이 MTTR의 주요 병목이다
2. **야간/주말 대응**: SRE 부재 시 보고서가 생성되어도 복구가 지연된다
3. **반복 장애**: 플레이북에 검증된 복구 절차가 있어도 매번 수동 실행해야 한다

초기 구현에서는 RCA 에이전트 파이프라인 내에 Remediation과 Verification 단계를 동기적으로 포함시켰으나, 운영 경험을 통해 문제점이 드러났다:

- **관심사 혼합**: RCA 에이전트가 분석과 복구를 모두 담당하여 단일 책임 원칙 위반
- **장애 전파**: 복구 실행 실패가 RCA 세션 자체를 FAILED로 만들 수 있음
- **독립 배포 불가**: 복구 로직 변경 시 RCA 에이전트 전체를 재배포해야 함
- **독립 스케일링 불가**: 복구 작업의 부하가 RCA 분석 리소스에 영향

## Decision

Remediation과 Verification을 RCA 에이전트에서 제거하고, **별도 Remediation 에이전트**로 분리한다. 두 시스템은 SNS → SQS 이벤트로 연결된다.

### 핵심 결정사항

1. **RCA 에이전트 책임 축소**: RCA 에이전트는 분석(F1~F6) → 보고서(F7) → 플레이북(F8) → 알림(F9)까지만 수행한다. 알림 시 플레이북 정보를 SNS 메시지에 포함한다.

2. **이벤트 기반 연결**: RCA 완료 시 기존 SNS Notification Topic에 플레이북을 포함한 메시지를 발행한다. Remediation 에이전트는 전용 SQS Queue로 이 이벤트를 구독한다.

3. **Remediation 에이전트 독립 배포**: 별도 ECS Fargate 태스크로 배포되어 RCA 에이전트와 독립적으로 스케일링, 배포, 장애 격리된다.

4. **복구 액션 범위**: MVP에서 지원하는 복구 액션은 두 가지로 제한한다:
   - **Fault Reset API 호출**: Healthcare 서비스의 `/fault/{type}/reset` 엔드포인트 호출로 주입된 장애를 해제한다
   - **ECS Force New Deployment**: 서비스 롤백을 위해 ECS UpdateService(forceNewDeployment)를 실행한다

5. **복구 후 검증**: Remediation 에이전트가 복구 실행 후 CloudWatch MCP를 통해 메트릭 정상화를 확인한다. 검증 결과를 DynamoDB에 기록하고 SNS로 알린다.

6. **피처 플래그 제어**: Remediation 에이전트의 실행 여부는 에이전트 자체의 desired count(0 또는 1)로 제어한다. RCA 에이전트는 항상 플레이북을 포함한 알림을 발행하며, 수신 여부는 Remediation 에이전트의 존재에 의존한다.

7. **서비스 디스커버리**: Cloud Map Private DNS를 통해 Healthcare 서비스의 내부 엔드포인트를 해석한다.

### 전체 흐름

```mermaid
flowchart LR
    RCA["RCA Agent<br/>(F1~F9)"] -->|SNS 발행<br/>(플레이북 포함)| SNS["SNS Topic"]
    SNS -->|SQS 구독| SQS["Remediation Queue"]
    SQS -->|Long Polling| REM["Remediation Agent"]
    REM -->|복구 실행| SVC["Healthcare Service"]
    REM -->|메트릭 검증| CW["CloudWatch MCP"]
    REM -->|결과 기록| DDB["DynamoDB"]
```

## Consequences

### Positive

- **관심사 분리**: RCA 에이전트는 분석에만 집중하고, 복구는 독립 생명주기를 가진다
- **장애 격리**: 복구 실패가 RCA 세션에 영향을 주지 않는다
- **독립 배포**: 복구 로직 변경 시 Remediation 에이전트만 재배포하면 된다
- **독립 스케일링**: 분석과 복구의 리소스 요구사항이 달라도 독립적으로 조정 가능하다
- **점진적 활성화**: Remediation 에이전트를 배포하지 않으면 기존과 동일하게 보고서 + 알림으로 종료된다

### Negative

- **인프라 복잡도 증가**: 별도 ECS 서비스, SQS Queue, IAM 역할이 추가된다
- **비동기 지연**: SNS → SQS 전달 지연이 추가되어 동기 실행 대비 복구 시작이 약간 느려진다
- **결과 추적 분산**: RCA 세션과 복구 결과가 다른 시점에 DynamoDB에 기록된다

### Risks

- **메시지 유실**: SQS DLQ를 설정하여 실패 메시지를 보존한다
- **중복 실행**: 멱등성 있는 복구 액션(reset API, force deploy)을 사용하여 중복 실행에 안전하다
- **검증 오판**: 메트릭 정상화에 시간이 걸려 검증 시점에 아직 복구가 반영되지 않을 수 있다. 검증 전 대기 시간을 포함한다

## Implementation Status

**미구현 — 설계만 완료된 상태.**

현재 구현된 부분:
- RCA 에이전트(Strands)가 F9(Notification)에서 플레이북을 포함한 SNS 알림을 발행 (**구현 완료**)
- `remediation.py` 모듈: Healthcare 장애 리셋 API 호출, ECS 강제 배포 로직 (**모듈 준비됨, 파이프라인 미연결**)
- `verification.py` 모듈: 복구 후 메트릭 정상화 검증 (**모듈 준비됨, 파이프라인 미연결**)

미구현 부분:
- Remediation Agent ECS Fargate 서비스 및 전용 SQS Queue (인프라)
- SNS → SQS 구독 설정
- Remediation Agent의 `main.py` 진입점 (SQS 폴링 → 복구 실행 → 검증)

CC Headless는 프롬프트 내에서 직접 복구를 수행하므로(10~11단계) 별도 Remediation Agent가 불필요하다.

## Related

- [ADR 0007: RCA 보고서 생성](./0007-rca-report-generation.md) — 복구 전 보고서 생성
- [ADR 0008: 플레이북 생성](./0008-playbook-generation.md) — 복구 액션의 근거
- [ADR 0009: 알림](./0009-notification.md) — 플레이북 포함 알림 발행
- [ADR 0010: 모델 티어 아키텍처](./0010-model-tier-architecture.md) — Verification은 Execution 티어 사용
- [ADR infra/0001: 알람 수신 아키텍처](../infra/0001-alarm-ingestion-sns-sqs-fargate.md) — SNS → SQS 패턴 재활용
- [ADR infra/0004: RDS + Healthcare 배포](../infra/0004-rds-healthcare-deployment.md) — Cloud Map 서비스 디스커버리
