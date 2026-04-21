# ADR 0003: 트레이스 분석 — X-Ray 기반 분산 트레이스 조회

Date: 2026-04-21

## Status

Proposed

## Context

서비스 간 호출 흐름에서 병목이나 실패 지점을 파악하려면 분산 트레이스 데이터가 필요하다. ADOT(AWS Distro for OpenTelemetry)로 계측된 서비스의 트레이스를 X-Ray에서 자동으로 조회해야 한다. Should-Have 기능으로, 트레이스 데이터가 없어도 다른 증거에 의존하여 RCA를 진행할 수 있다.

## Decision

**X-Ray 전용 AWS Labs MCP 서버가 공개되면 채택한다.** 현시점에서 X-Ray MCP 서버가 제공되지 않으므로, MVP에서는 커스텀 @tool로 X-Ray API를 래핑하거나 v2에서 구현을 검토한다.

### 핵심 결정사항

1. **MVP 방향**: X-Ray MCP 서버가 없으므로 MVP에서는 트레이스 분석을 생략하고, 메트릭/로그/배포이력 증거로 RCA를 진행한다.

2. **분석 포인트**: 향후 구현 시 응답 시간 이상치, 에러/폴트 세그먼트, 서비스 간 호출 지연 패턴을 식별한다.

3. **ADOT 기반 계측**: OTel 표준 기반 계측으로 X-Ray 백엔드에서 트레이스를 조회한다.

## Consequences

### Positive

- 다른 MCP 서버와 동일한 패턴으로 확장 가능
- MVP 범위를 줄여 핵심 기능에 집중 가능

### Negative

- MVP에서 트레이스 기반 증거가 없어 서비스 간 호출 흐름 분석 불가
- ADOT 계측이 되지 않은 서비스에서는 트레이스 데이터 자체가 없음

### Risks

- X-Ray MCP 서버가 장기간 제공되지 않을 경우 커스텀 @tool 구현이 필요하다.

## Related

- [ADR tools/0001: 메트릭 수집](0001-metrics-collection.md) — 동일 가설의 다른 유형 증거 수집
