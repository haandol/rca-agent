# ADR 0001: 메트릭 수집 — CloudWatch MCP 서버 기반 증거 수집

Date: 2026-04-21

## Status

Accepted

## Context

가설 검증에 필요한 CloudWatch 메트릭(CPU, Memory, 커넥션 수, Latency, Error Rate 등)을 자동으로 수집해야 한다. 수동으로 콘솔에서 메트릭을 조회하는 과정을 에이전트가 대체한다.

## Decision

**AWS Labs CloudWatch MCP 서버(`awslabs/cloudwatch-mcp-server`)**를 사용한다. 커스텀 @tool을 직접 구현하지 않고, AWS에서 공식 제공하는 MCP 서버의 도구를 Strands 에이전트에 연결하여 사용한다.

### 핵심 결정사항

1. **MCP 서버**: `awslabs/cloudwatch-mcp-server`를 Strands 에이전트의 MCP 서버로 등록한다. 메트릭 관련 도구 `get_metric_data`, `get_metric_metadata`, `analyze_metric`을 활용한다.

2. **비교 분석**: 장애 시점 메트릭과 직전 24시간 동일 시간대 메트릭 비교는 에이전트 프롬프트에서 `get_metric_data`를 두 번 호출하도록 지시한다.

3. **알람 분석**: `get_active_alarms`, `get_alarm_history` 도구로 현재 활성 알람과 이력을 조회하여 초기 스코핑에 활용한다.

4. **메트릭 트렌드 분석**: `analyze_metric` 도구로 트렌드, 계절성, 통계적 속성을 자동 분석한다.

5. **증거 저장**: 수집된 메트릭을 JSON 형태로 S3에 저장하고, DynamoDB에 증거 메타데이터를 기록한다.

## Consequences

### Positive

- AWS 공식 MCP 서버 사용으로 커스텀 코드 유지보수 불필요
- 메트릭 조회 + 알람 분석 + 트렌드 분석 등 풍부한 도구셋 즉시 활용
- Strands SDK의 MCP 네이티브 지원으로 간단한 서버 등록만으로 연동

### Negative

- MCP 서버 버전 업데이트에 의존성 발생
- 커스텀 로직(Period 자동 확장 등)은 에이전트 프롬프트로 제어해야 함

### Risks

- MCP 서버 업데이트로 도구 인터페이스가 변경될 수 있다. 버전을 고정하여 완화한다.

## Related

- [ADR agent/0018: 가설 트리 라이프사이클](../agent/0018-hypothesis-tree-lifecycle.md) — 우선순위 결정 단계에서 메트릭 수집 도구 호출을 지정
