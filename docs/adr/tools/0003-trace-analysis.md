# ADR 0003: 트레이스 분석 — X-Ray 기반 분산 트레이스 조회

Date: 2026-04-21

## Status

Rejected

## Context

서비스 간 호출 흐름에서 병목이나 실패 지점을 파악하려면 분산 트레이스 데이터가 필요하다. ADOT(AWS Distro for OpenTelemetry)로 계측된 서비스의 트레이스를 X-Ray에서 자동으로 조회해야 한다. Should-Have 기능으로, 트레이스 데이터가 없어도 다른 증거에 의존하여 RCA를 진행할 수 있다.

## Decision

**X-Ray 기반 트레이스 분석을 채택하지 않는다.** X-Ray 전용 MCP 서버가 제공되지 않고, X-Ray API를 직접 래핑하더라도 트레이스 데이터를 에이전트가 활용하기에 불편하여 실효성이 낮다. 메트릭/로그/배포이력/코드 변경 증거만으로 RCA를 진행한다.

## Consequences

### Positive

- 다른 MCP 서버와 동일한 패턴으로 확장 가능
- MVP 범위를 줄여 핵심 기능에 집중 가능

### Negative

- MVP에서 트레이스 기반 증거가 없어 서비스 간 호출 흐름 분석 불가
- ADOT 계측이 되지 않은 서비스에서는 트레이스 데이터 자체가 없음

### Risks

- 트레이스 기반 증거 없이 서비스 간 호출 흐름을 분석해야 하므로, 분산 시스템 장애에서 정확도가 낮아질 수 있다. CloudWatch 메트릭과 로그로 보완한다.

## Related

- [ADR tools/0001: 메트릭 수집](0001-metrics-collection.md) — 동일 가설의 다른 유형 증거 수집
