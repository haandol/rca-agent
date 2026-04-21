# ADR 0002: 로그 검색 — CloudWatch Logs Insights 기반 증거 수집

Date: 2026-04-21

## Status

Proposed

## Context

가설 검증에 필요한 에러 로그와 관련 로그를 자동으로 검색해야 한다. LLM이 가설에 맞는 검색 쿼리를 자동 생성하여 SRE가 직접 Logs Insights 쿼리를 작성하지 않아도 핵심 로그 증거가 수집되도록 한다.

## Decision

**CloudWatch Logs Insights 기반 MCP 도구 + LLM 쿼리 자동 생성** 전략을 채택한다.

### 핵심 결정사항

1. **MCP 도구 인터페이스**: `query_logs(log_group, query_string, start_time, end_time)` 형태의 도구를 제공한다.

2. **LLM 쿼리 생성**: LLM이 가설에 맞는 Logs Insights 쿼리를 자동 생성한다. 예를 들어 가설이 "DB 커넥션 누수"면 `fields @timestamp, @message | filter @message like /connection|Too many/` 형태의 쿼리를 생성한다.

3. **로그 요약**: 대량 로그 반환 시 LLM이 핵심 로그만 추출하여 요약한다.

4. **재시도 전략**: 쿼리 타임아웃(60초) 시 시간 범위를 줄여 재시도한다. 쿼리 결과 0건 시 LLM이 키워드를 변경하여 1회 재시도한다.

5. **MVP 범위**: CloudWatch Logs Insights만 사용한다. OpenSearch 기반 대용량 로그 검색은 v2에서 검토한다.

## Consequences

### Positive

- LLM이 가설에 적합한 쿼리를 자동 생성하여 SRE의 쿼리 작성 부담 제거
- 핵심 로그 자동 추출로 대량 로그에서 증거 식별 시간 단축

### Negative

- LLM이 생성한 쿼리가 비효율적이거나 부정확할 수 있음
- CloudWatch Logs Insights의 쿼리 문법 제약으로 복잡한 검색이 어려울 수 있음

### Risks

- 로그 그룹이 존재하지 않거나 로그 보존 기간이 짧은 경우 증거 수집 불가. "수집 불가"로 표시하고 다른 증거에 의존하여 완화한다.

## Related

- [ADR tools/0001: 메트릭 수집](0001-metrics-collection.md) — 동일 가설의 다른 유형 증거 수집
