# ADR 0012: 자동 복구(Remediation) — 분석 결과 기반 closed-loop 장애 복구

Date: 2026-04-23

## Status

Accepted

## Context

기존 RCA 에이전트(agent/0001~0009)는 분석 전용 시스템으로, 근본 원인을 파악하고 보고서와 플레이북을 생성하면 파이프라인이 종료된다. 실제 복구는 SRE가 보고서를 읽고 수동으로 수행해야 한다.

이 구조의 한계:

1. **MTTR 병목**: 분석은 자동화되었지만, 복구 실행까지의 지연이 MTTR의 주요 병목이다
2. **야간/주말 대응**: SRE 부재 시 보고서가 생성되어도 복구가 지연된다
3. **반복 장애**: 플레이북에 검증된 복구 절차가 있어도 매번 수동 실행해야 한다

자동 복구를 추가하면 "분석 → 플레이북 생성 → 플레이북 실행 → 검증"의 closed-loop을 완성할 수 있다. 단, 자동 복구는 리스크가 높으므로 피처 플래그로 on/off 제어하고, 안전한 복구 액션만 실행한다.

## Decision

파이프라인에 **Remediation(F10)**과 **Verification(F11)** 단계를 추가하여 closed-loop 자동 복구를 구현한다. 기존 F1~F9는 유지하고, Notification이 F12로 이동한다.

### 핵심 결정사항

1. **피처 플래그 제어**: `REMEDIATION_ENABLED` 환경변수(기본 `false`)로 자동 복구를 on/off 한다. 비활성 시 기존과 동일하게 보고서 + 알림으로 종료된다.

2. **복구 액션 범위**: MVP에서 지원하는 복구 액션은 두 가지로 제한한다:
   - **Fault Reset API 호출**: Healthcare 서비스의 `/fault/{type}/reset` 엔드포인트 호출로 주입된 장애를 해제한다
   - **ECS Force New Deployment**: 서비스 롤백을 위해 ECS UpdateService(forceNewDeployment)를 실행한다

3. **서비스 디스커버리**: Cloud Map Private DNS(`healthcare.rcaagentdev.local`)를 통해 Healthcare 서비스의 내부 엔드포인트를 해석한다. VPC 내 Service Connect로 접근한다.

4. **복구 후 검증**: Verification 단계에서 CloudWatch MCP를 통해 메트릭 정상화를 확인한다. 복구가 성공했는지 객관적으로 검증한 후 알림을 발송한다.

5. **LLM 역할 분리**: Remediation은 LLM 없이 순수 로직으로 실행하고(플레이북 파싱 → API 호출), Verification은 Execution 티어(Haiku 4.5) + CloudWatch MCP로 메트릭을 재확인한다.

6. **영구 지속형 장애 주입**: 기존 장애 주입(high-cpu, slow-query)이 시간 기반으로 자동 종료되었으나, 자동 복구 테스트를 위해 명시적 reset API 호출까지 지속되도록 변경한다.

### 파이프라인 전체 흐름 (12단계)

```
F1(Scoping) → F2(Hypothesis) → F3(Prioritization) → F4(Evidence) → F5(Validation) →
F6(Branching) → F7(Termination) → F8(Report) → F9(Playbook) →
F10(Remediation) → F11(Verification) → F12(Notification)
```

## Consequences

### Positive

- **MTTR 단축**: 분석 완료 후 자동 복구까지 수행하여 사람 개입 없이 장애를 해결할 수 있다
- **Closed-loop 검증**: 복구 후 메트릭 정상화를 자동 확인하여 복구 성공 여부를 객관적으로 판단한다
- **점진적 활성화**: 피처 플래그로 환경별(dev/staging/prod) 독립 제어가 가능하다
- **플레이북 재활용**: 기존 플레이북 생성(agent/0008) 결과를 복구 액션으로 직접 활용한다

### Negative

- **복구 리스크**: 잘못된 복구 액션이 장애를 악화시킬 수 있다. MVP에서는 안전한 액션(reset API, force deploy)만 허용하여 리스크를 최소화한다
- **의존성 증가**: Healthcare 서비스의 reset API 가용성과 ECS API 권한에 의존한다

### Risks

- **부분 복구**: 여러 복구 액션 중 일부만 성공할 수 있다. RemediationResult에 액션별 성공/실패를 기록하고, 부분 실패 시에도 알림을 발송한다
- **검증 오판**: 메트릭 정상화에 시간이 걸려 검증 시점에 아직 복구가 반영되지 않을 수 있다. 검증 에이전트에 대기 시간을 포함한다

## Related

- [ADR 0007: RCA 보고서 생성](./0007-rca-report-generation.md) — 복구 전 보고서 생성
- [ADR 0008: 플레이북 생성](./0008-playbook-generation.md) — 복구 액션의 근거
- [ADR 0009: 알림](./0009-notification.md) — 복구 결과 포함 알림 (F12로 이동)
- [ADR 0010: 모델 티어 아키텍처](./0010-model-tier-architecture.md) — Verification은 Execution 티어 사용
- [ADR infra/0004: RDS + Healthcare 배포](../infra/0004-rds-healthcare-deployment.md) — Cloud Map 서비스 디스커버리
