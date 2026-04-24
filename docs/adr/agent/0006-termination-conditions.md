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
6. **외부 취소 (CANCELLED)**: 대시보드에서 관리자가 세션 상태를 `CANCELLED`로 변경하면, 다음 `update_state()` 호출 시점에 파이프라인이 즉시 종료된다.

### 핵심 결정사항

1. **순수 로직 (LLM 미사용)**: `check_termination()` 함수는 LLM을 호출하지 않고 순수 로직으로 중단 조건을 평가한다. `time.monotonic()` 기반으로 경과 시간을 계산하고, 가설 목록에서 최대 depth를 추출한다.

2. **정상 중단**: CONFIRMED 가설 중 confidence ≥ 0.9인 가설이 있으면 해당 가설을 `best_hypothesis`로 설정하고 보고서 생성에 진입한다.

3. **강제 중단**: 시간/깊이/반복 한도 초과 시 `_best_hypothesis()` 함수가 모든 judgment 중 가장 높은 confidence_score를 가진 가설을 선택하여 보고서 생성에 전달한다.

4. **전체 기각 시 재생성**: 모든 가설이 기각되면 `check_termination()`에서 종료하지 않고, `main.py`의 검증 루프가 가설을 재생성한다(최대 2회, `RCA_MAX_REGENERATION_ROUNDS`). 재생성 한도를 초과하면 루프가 종료되고 "근본 원인 미확정" 상태로 보고서를 생성한다.

5. **OR 평가 순서**: CONFIRMED → TIME_BUDGET → MAX_DEPTH → MAX_LOOPS 순서로 평가하며, 첫 번째로 충족된 조건에서 즉시 반환한다. 전체 기각(ALL_REJECTED)은 `check_termination()`이 아닌 메인 루프에서 재생성 로직으로 처리한다.

6. **Cooperative Cancellation**: `update_state()`에 DynamoDB ConditionExpression(`#st <> CANCELLED`)을 추가하여, 세션이 CANCELLED 상태이면 `SessionCancelledError`를 발생시킨다. 파이프라인은 이 예외를 잡아 `mark_failed` 없이 조용히 종료한다. 별도 폴링 없이 매 단계 전환 시 자연스럽게 취소가 감지된다.

## Consequences

### Positive

- 다중 중단 조건으로 운영 통제 확보 — 비용과 시간의 예측 가능성 확보
- 정상/강제 중단 분리로 최적 시나리오와 최악 시나리오 모두 처리
- 미확정 시에도 보고서가 생성되어 SRE가 수동으로 이어서 분석 가능

### Negative

- 시간 예산(20분)이 복잡한 장애에 부족할 수 있음
- 강제 중단 시 근본 원인이 확정되지 않은 보고서가 생성될 수 있음
- CANCELLED 시 진행 중이던 LLM 호출은 즉시 중단되지 않고, 해당 호출이 완료된 뒤 다음 단계 전환에서 종료됨

### Risks

- 중단 조건 임계치가 너무 느슨하면 비용 폭증, 너무 엄격하면 정확도 저하. MVP 운영 데이터로 조정한다.

## Implementation Notes

- `check_termination()`: CONFIRMED, TIME_BUDGET, MAX_DEPTH, MAX_LOOPS만 평가. ALL_REJECTED는 메인 루프에서 재생성으로 처리.
- **미검증 가설 자동 기각**: 루프 종료 후 보고서 생성 전에, PENDING/NEEDS_INVESTIGATION 상태의 가설을 REJECTED로 일괄 기각한다. `judgment_reasoning`에 "리소스 제약으로 검증 미완료 — 분석 종료 시 자동 기각"을 기록한다. best_hypothesis는 제외. 이를 통해 세션 COMPLETED 시 모든 가설이 최종 상태(CONFIRMED/REJECTED)를 갖는다.
- `update_state()`: `ConditionExpression: #st <> :cancelled`로 cooperative cancellation 구현. `ConditionalCheckFailedException` → `SessionCancelledError` 변환.
- `_process_alarm()` / `_run_rca()`: `SessionCancelledError`를 별도 `except` 블록에서 처리하여 `mark_failed` 없이 종료.
- `mark_completed()` / `mark_failed()`: `AND #state <> :cancelled` ConditionExpression으로 CANCELLED 상태의 세션이 COMPLETED/FAILED로 전이되지 않도록 가드.
- 대시보드: `POST /api/sessions/:id/cancel` 엔드포인트가 DDB 세션 상태를 CANCELLED로 업데이트. terminal 상태(`COMPLETED`, `FAILED`, `CANCELLED`, `OUTDATED`)인 세션은 취소 불가 (409 응답).

## Related

- [ADR agent/0004: 가설 검증 및 가지치기](0004-hypothesis-validation-pruning.md) — 매 검증 루프 후 중단 조건을 평가
