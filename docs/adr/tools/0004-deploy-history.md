# ADR 0004: 배포 이력 조회 — CloudTrail MCP 서버 기반 배포/변경 이벤트 조회

Date: 2026-04-21

## Status

Accepted

## Context

장애와 최근 배포의 상관관계를 분석하려면 배포/변경 이력을 자동으로 조회해야 한다. 단순히 시간 범위로 컷오프하면 느린 누수(커넥션 릭, 메모리 릭)처럼 배포 후 수시간~수일 후에 발현되는 장애 원인을 놓칠 수 있다.

## Decision

**AWS Labs CloudTrail MCP 서버(`awslabs/cloudtrail-mcp-server`)**를 사용한다. 커스텀 @tool을 직접 구현하지 않고, AWS에서 공식 제공하는 MCP 서버의 도구를 Strands 에이전트에 연결하여 사용한다.

### 핵심 결정사항

1. **MCP 서버**: `awslabs/cloudtrail-mcp-server`를 Strands 에이전트의 MCP 서버로 등록한다. `lookup_events` 도구로 CloudTrail 이벤트를 조회한다.

2. **배포 이벤트 필터링**: 에이전트 프롬프트에서 `lookup_events`에 리소스 이름 기반으로 조회하고, 배포 관련 이벤트(CreateDeployment, UpdateService, RegisterTaskDefinition 등)만 필터링하도록 지시한다.

3. **시간 기반 컷오프 없음**: 직전 N개 배포 이벤트를 확인하도록 에이전트 프롬프트에서 지시한다. 90일 이내 관리 이벤트를 조회할 수 있다.

4. **상관관계 분석**: 에이전트가 배포 시각과 메트릭 이상 시작 시각을 비교하여 가장 상관성 높은 배포를 LLM 추론으로 식별한다.

5. **고급 분석(선택)**: CloudTrail Lake가 활성화된 환경에서는 `lake_query` 도구로 SQL 기반 고급 분석이 가능하다.

## Consequences

### Positive

- AWS 공식 MCP 서버 사용으로 커스텀 코드 유지보수 불필요
- CloudTrail Lake SQL 쿼리까지 지원하여 향후 고급 분석 확장 가능
- lookup_events의 90일 이벤트 조회로 느린 누수 장애의 원인도 탐지 가능

### Negative

- 배포 이벤트 필터링 로직이 에이전트 프롬프트에 의존하여 일관성이 떨어질 수 있음
- CloudTrail 이벤트가 최대 15분 지연될 수 있음

### Risks

- 조회 기간 내 배포 이벤트가 없는 경우 에이전트가 "배포 없음"으로 기록하여 배포 관련 가설 기각 근거로 활용한다.

## Related

- [ADR tools/0001: 메트릭 수집](0001-metrics-collection.md) — 상관관계 분석에 메트릭 이상 시점 데이터를 제공
