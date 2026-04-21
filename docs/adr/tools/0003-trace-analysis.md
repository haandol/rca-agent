# ADR 0003: 트레이스 분석 — X-Ray 기반 분산 트레이스 조회

Date: 2026-04-21

## Status

Proposed

## Context

서비스 간 호출 흐름에서 병목이나 실패 지점을 파악하려면 분산 트레이스 데이터가 필요하다. ADOT(AWS Distro for OpenTelemetry)로 계측된 서비스의 트레이스를 X-Ray에서 자동으로 조회해야 한다. Should-Have 기능으로, 트레이스 데이터가 없어도 다른 증거에 의존하여 RCA를 진행할 수 있다.

## Decision

**X-Ray API 기반 MCP 도구**를 구현한다.

### 핵심 결정사항

1. **MCP 도구 인터페이스**: `query_traces(service_name, start_time, end_time, filter_expression)` 형태의 도구를 제공한다. X-Ray BatchGetTraces / GetTraceSummaries API를 래핑한다.

2. **분석 포인트**: 응답 시간 이상치, 에러/폴트 세그먼트, 서비스 간 호출 지연 패턴을 식별한다.

3. **ADOT 기반 계측**: OTel 표준 기반 계측으로 X-Ray 백엔드에서 트레이스를 조회한다.

4. **샘플링 보완**: 트레이스 샘플링으로 관련 트레이스가 누락된 경우 시간 범위를 확장하여 재시도한다.

## Consequences

### Positive

- 서비스 간 호출 흐름 분석으로 다른 증거로는 파악하기 어려운 의존 서비스 장애, 지연 병목 식별 가능
- OTel 표준 기반으로 계측 확장성 확보

### Negative

- ADOT 계측이 되지 않은 서비스에서는 트레이스 데이터 자체가 없음
- 트레이스 샘플링으로 장애 시점의 정확한 트레이스를 놓칠 수 있음

### Risks

- X-Ray에 트레이스 데이터가 없는 경우 "수집 불가"로 표시하고 다른 증거에 의존한다. Should-Have 기능이므로 RCA 전체 흐름에 영향을 주지 않는다.

## Related

- [ADR tools/0001: 메트릭 수집](0001-metrics-collection.md) — 동일 가설의 다른 유형 증거 수집
