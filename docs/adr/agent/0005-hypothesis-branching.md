# ADR 0005: 하위 가설 분기 — 트리 동적 확장

Date: 2026-04-21

## Status

Accepted

## Context

추가조사가 필요한 가설(`NEEDS_INVESTIGATION`)에 대해 더 구체적인 하위 가설을 생성하여 근본 원인을 점진적으로 좁혀가야 한다. 무제한 분기는 탐색 폭발을 야기하므로 적절한 제어가 필요하다.

## Decision

**LLM 기반 하위 가설 2~3개 생성 + 깊이 제한** 전략을 채택한다.

### 핵심 결정사항

1. **Strands SDK structured output**: `BranchingOutput` Pydantic 모델을 `structured_output_model`로 지정하여 LLM이 `description`, `category`, `confidence_score`, `required_evidence`를 포함한 자식 가설 목록을 반환한다. 비스트리밍 모드로 호출한다.

2. **트리 확장**: 자식 노드에 UUID 기반 `hypothesis_id`를 부여하고, `parent_id`를 부모 가설 ID로, `depth`를 `parent.depth + 1`로 설정한다. 부모 가설과 수집된 증거, 기각된 가설 목록을 LLM에 전달한다.

3. **깊이 제한**: 트리 최대 깊이를 기본 3(`MAX_BRANCHING_DEPTH`)으로 설정한다. `parent.depth >= max_depth`이면 LLM을 호출하지 않고 빈 결과를 반환한다.

4. **검증 루프 재진입**: 오케스트레이션 레이어(`main.py`)에서 새 자식 가설을 가설 목록에 추가하고 우선순위 → 검증 → 종료 판단 루프를 재진입한다.

5. **중복 방지**: `_is_duplicate()` 함수로 부모 가설 및 기각된 가설과의 대소문자 무시 비교를 수행하여 중복 자식을 자동 제거한다. 중복 제거 후 유효한 자식만 반환한다.

6. **타임아웃 및 fallback**: `ThreadPoolExecutor` 120초 타임아웃을 적용하며, 실패 시 빈 자식 목록을 반환하여 루프가 자연스럽게 종료되도록 한다.

## Consequences

### Positive

- 점진적 탐색으로 초기 가설이 부정확해도 하위 분기를 통해 근본 원인에 수렴 가능
- 깊이 제한으로 무한 탐색 방지

### Negative

- 분기 시마다 LLM 호출 비용과 증거 수집 비용이 누적
- 트리 구조가 복잡해지면 보고서 생성 시 추론 경로 설명이 길어질 수 있음

### Risks

- 깊이 제한이 너무 얕으면 복잡한 장애의 근본 원인에 도달하지 못할 수 있다. 운영 데이터를 바탕으로 깊이 제한을 조정한다.

## Related

- [ADR agent/0004: 가설 검증 및 가지치기](0004-hypothesis-validation-pruning.md) — `NEEDS_INVESTIGATION` 판단을 내리는 이전 단계
