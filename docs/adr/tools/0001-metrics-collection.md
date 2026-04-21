# ADR 0001: 메트릭 수집 — CloudWatch GetMetricData 기반 증거 수집

Date: 2026-04-21

## Status

Proposed

## Context

가설 검증에 필요한 CloudWatch 메트릭(CPU, Memory, 커넥션 수, Latency, Error Rate 등)을 자동으로 수집해야 한다. 수동으로 콘솔에서 메트릭을 조회하는 과정을 에이전트가 대체한다.

## Decision

**CloudWatch GetMetricData API 기반 MCP 도구**를 구현한다.

### 핵심 결정사항

1. **MCP 도구 인터페이스**: `query_metrics(namespace, metric_name, dimensions, start_time, end_time, period)` 형태의 도구를 제공한다.

2. **조회 범위**: 알람 발생 전후 1시간, 기본 1분 간격(Period=60). 데이터 포인트가 부족한 경우 Period를 5분으로 확장한다.

3. **비교 분석**: 장애 시점 메트릭과 직전 24시간 동일 시간대 메트릭을 비교하여 이상 탐지에 활용한다.

4. **증거 저장**: 수집된 메트릭을 JSON 형태로 S3에 저장하고, DynamoDB에 증거 메타데이터를 기록한다.

5. **쓰로틀링 대응**: CloudWatch API 쓰로틀링 시 exponential backoff로 재시도한다. 메트릭이 존재하지 않는 경우 해당 증거 항목을 "수집 불가"로 표시한다.

## Consequences

### Positive

- 가설 검증에 필요한 메트릭을 자동으로 수집하여 수동 콘솔 조회 불필요
- 비교 분석으로 이상 시점을 객관적으로 식별 가능
- 구조화된 JSON 저장으로 보고서 생성 시 증거 참조 용이

### Negative

- CloudWatch API 호출 비용이 메트릭 수량에 비례하여 발생
- 커스텀 메트릭이 미설정된 리소스에서는 수집 가능 항목이 제한적

### Risks

- API 쓰로틀링이 심한 환경에서 메트릭 수집이 지연될 수 있다. 재시도 간격을 조정하고 핵심 메트릭 우선 수집으로 완화한다.

## Related

- [ADR agent/0003: 가설 우선순위 결정](../agent/0003-hypothesis-prioritization.md) — 검증 계획에서 메트릭 수집 도구 호출을 지정
