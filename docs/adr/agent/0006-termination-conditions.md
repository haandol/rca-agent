# ADR 0006: 중단 조건 판단 — 신뢰도·비용·깊이 기반 탐색 종료

Date: 2026-04-21

## Status

Accepted

## Context

가설-트리 탐색이 무한히 계속되면 시간과 비용이 폭증하고 운영 통제가 불가능해진다. 적절한 시점에 탐색을 종료하고 보고서 생성으로 전환해야 한다.

## Decision

**OR 조건 기반 다중 중단 조건**을 채택한다. 하나라도 충족되면 탐색을 종료한다.

### 중단 조건

1. **신뢰도 임계치**: CONFIRMED 가설의 confidence_score가 0.9 이상
2. **시간 예산**: RCA 시작 후 20분 경과 (`RCA_TIME_BUDGET_SECONDS=1200`)
3. **비용 예산**: LLM 토큰 사용량이 사전 설정 한도 초과 — `TerminationReason.TOKEN_BUDGET` enum이 정의되어 있으나, Strands SDK의 토큰 사용량 추적 API가 확정되면 구현 예정 (현재 보류)
4. **최대 깊이**: 가설 트리 깊이가 5를 초과 (`RCA_MAX_TREE_DEPTH=5`)
5. **최대 반복**: 검증 루프가 3회를 초과 (`RCA_MAX_VALIDATION_LOOPS=3`)

### 핵심 결정사항

1. **순수 로직 (LLM 미사용)**: `check_termination()` 함수는 LLM을 호출하지 않고 순수 로직으로 중단 조건을 평가한다. `time.monotonic()` 기반으로 경과 시간을 계산하고, 가설 목록에서 최대 depth를 추출한다.

2. **정상 중단**: CONFIRMED 가설 중 confidence ≥ 0.9인 가설이 있으면 해당 가설을 `best_hypothesis`로 설정하고 보고서 생성에 진입한다.

3. **강제 중단**: 시간/깊이/반복 한도 초과 시 `_best_hypothesis()` 함수가 모든 judgment 중 가장 높은 confidence_score를 가진 가설을 선택하여 보고서 생성에 전달한다.

4. **근본 원인 미확정 처리**: 모든 가설이 기각되고 추가 가설 생성(최대 2회, `RCA_MAX_REGENERATION_ROUNDS`)으로도 확정하지 못한 경우 `TerminationReason.ALL_REJECTED`로 중단하고 "근본 원인 미확정" 상태로 보고서를 생성한다.

5. **OR 평가 순서**: CONFIRMED → TIME_BUDGET → MAX_DEPTH → MAX_LOOPS → ALL_REJECTED 순서로 평가하며, 첫 번째로 충족된 조건에서 즉시 반환한다.

## Consequences

### Positive

- 다중 중단 조건으로 운영 통제 확보 — 비용과 시간의 예측 가능성 확보
- 정상/강제 중단 분리로 최적 시나리오와 최악 시나리오 모두 처리
- 미확정 시에도 보고서가 생성되어 SRE가 수동으로 이어서 분석 가능

### Negative

- 시간 예산(20분)이 복잡한 장애에 부족할 수 있음
- 강제 중단 시 근본 원인이 확정되지 않은 보고서가 생성될 수 있음

### Risks

- 중단 조건 임계치가 너무 느슨하면 비용 폭증, 너무 엄격하면 정확도 저하. MVP 운영 데이터로 조정한다.

## Related

- [ADR agent/0004: 가설 검증 및 가지치기](0004-hypothesis-validation-pruning.md) — 매 검증 루프 후 중단 조건을 평가
