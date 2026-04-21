# ADR 0002: 로그 검색 — CloudWatch MCP 서버 기반 증거 수집

Date: 2026-04-21

## Status

Accepted

## Context

가설 검증에 필요한 에러 로그와 관련 로그를 자동으로 검색해야 한다. LLM이 가설에 맞는 검색 쿼리를 자동 생성하여 SRE가 직접 Logs Insights 쿼리를 작성하지 않아도 핵심 로그 증거가 수집되도록 한다.

## Decision

**AWS Labs CloudWatch MCP 서버(`awslabs/cloudwatch-mcp-server`)**의 로그 관련 도구를 사용한다. 커스텀 @tool을 직접 구현하지 않는다.

### 핵심 결정사항

1. **MCP 서버**: 메트릭 수집(ADR 0001)과 동일한 `awslabs/cloudwatch-mcp-server`를 사용한다. 로그 관련 도구 `describe_log_groups`, `execute_log_insights_query`, `get_logs_insight_query_results`, `cancel_logs_insight_query`, `analyze_log_group`을 활용한다.

2. **LLM 쿼리 생성**: Strands 에이전트가 가설에 맞는 Logs Insights 쿼리를 생성하고 `execute_log_insights_query`로 실행한다.

3. **비동기 쿼리**: Logs Insights는 2단계(쿼리 실행 → 결과 조회)로 동작한다. 에이전트가 `execute_log_insights_query` 후 `get_logs_insight_query_results`로 결과를 폴링한다.

4. **이상 패턴 분석**: `analyze_log_group` 도구로 로그 그룹의 이상 탐지, 메시지 패턴, 에러 패턴을 자동 분석한다.

5. **MVP 범위**: CloudWatch Logs Insights만 사용한다. OpenSearch 기반 대용량 로그 검색은 v2에서 검토한다.

## Consequences

### Positive

- 하나의 MCP 서버로 메트릭 + 로그를 모두 커버하여 관리 포인트 최소화
- 로그 이상 탐지(`analyze_log_group`)를 추가 구현 없이 활용 가능
- LLM이 쿼리를 자동 생성하여 SRE의 쿼리 작성 부담 제거

### Negative

- MCP 서버가 쿼리 타임아웃 시 자동 재시도를 제공하지 않음 — 에이전트 프롬프트로 제어 필요
- LLM이 생성한 쿼리가 비효율적이거나 부정확할 수 있음

### Risks

- 로그 그룹이 존재하지 않거나 로그 보존 기간이 짧은 경우 증거 수집 불가. "수집 불가"로 표시하고 다른 증거에 의존하여 완화한다.

## Related

- [ADR tools/0001: 메트릭 수집](0001-metrics-collection.md) — 동일 MCP 서버의 메트릭 도구
