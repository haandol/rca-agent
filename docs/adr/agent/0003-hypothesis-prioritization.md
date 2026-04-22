# ADR 0003: 가설 우선순위 결정 — LLM 기반 우선순위 및 검증 계획 수립

Date: 2026-04-21

## Status

Accepted

## Context

가설이 생성된 후 어떤 순서로 검증할지 결정해야 한다. 무작위 순서로 검증하면 시간과 비용이 낭비되며, 가장 가능성 높은 원인의 확인이 지연된다.

검토한 대안:

- **고정 순서**: 카테고리 기본 순서(배포 > 인프라 > 트래픽)로 항상 검증 — 단순하나 컨텍스트 무시
- **LLM 동적 우선순위**: 알람 유형, 스코핑 결과, 가설 카테고리를 종합하여 LLM이 검증 순서 결정
- **신뢰도 기반 정렬**: 초기 confidence_score 내림차순 — LLM 판단 없이 기계적 정렬

## Decision

**LLM 동적 우선순위 결정 + 검증 계획 수립** 전략을 채택한다.

### 핵심 결정사항

1. **Strands SDK structured output**: `PrioritizationOutput` Pydantic 모델을 `structured_output_model`로 지정하여 LLM이 각 가설의 `priority_rank`, `tools`, `estimated_seconds`, `parallel_group`을 구조화된 형태로 반환한다. 비스트리밍 모드로 호출한다.

2. **LLM 기반 우선순위**: 알람 유형, 스코핑 결과, 가설 카테고리를 종합하여 LLM이 검증 순서를 결정한다. 예를 들어 최근 배포가 확인되면 배포 관련 가설을 우선 검증한다.

3. **검증 계획 수립**: 각 가설에 대해 우선순위 순서, 필요한 도구 호출 목록(메트릭 조회, 로그 검색 등), 예상 소요 시간을 포함한 검증 계획을 수립한다.

4. **병렬 검증 판단**: 독립적인 가설은 동일 `parallel_group` 번호를 부여하여 병렬 검증 가능하도록 표시한다.

5. **카테고리 기반 fallback**: LLM 호출이 타임아웃되거나 실패하면 카테고리 기본 순서(DEPLOYMENT > INFRASTRUCTURE > TRAFFIC > DEPENDENCY > CONFIGURATION)로 정렬하여 fallback 결과를 반환한다. `ThreadPoolExecutor`로 120초 타임아웃을 강제한다.

## Consequences

### Positive

- 컨텍스트에 맞는 검증 순서로 근본 원인에 빠르게 수렴
- 병렬 검증 판단으로 독립적 가설의 검증 시간 단축
- 각 가설별 명확한 검증 계획으로 도구 호출 효율화

### Negative

- LLM 호출 비용이 추가로 발생
- LLM의 우선순위 판단이 항상 최적이 아닐 수 있음

### Risks

- 사용 불가능한 도구가 검증 계획에 포함될 수 있다. 해당 도구를 제외하고 대체 방법을 제안하여 완화한다.

## Related

- [ADR agent/0002: 가설 생성](0002-hypothesis-generation.md) — 우선순위를 매길 가설을 생성하는 단계
