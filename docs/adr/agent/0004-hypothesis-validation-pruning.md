# ADR 0004: 가설 검증 및 가지치기 — 증거 기반 확정/기각 판단

Date: 2026-04-21

## Status

Accepted

## Context

수집된 증거를 바탕으로 각 가설의 타당성을 판단하고, 불필요한 분기를 제거하여 근본 원인으로 빠르게 수렴해야 한다. 판단 기준이 모호하면 불필요한 탐색이 계속되거나 실제 원인이 조기에 기각될 수 있다.

검토한 대안:

- **규칙 기반 판단**: 메트릭 임계치 등 정량 규칙으로 기계적 판단 — 단순하나 복합적 증거 종합 불가
- **LLM 신뢰도 기반 판단**: LLM이 가설과 증거 간 일치도를 평가하여 신뢰도(confidence_score)를 산출
- **하이브리드**: 정량 규칙 1차 필터 + LLM 2차 판단 — 복잡도 증가 대비 MVP에서 이점 불분명

## Decision

**LLM 신뢰도 기반 확정/기각/추가조사 3단 판단** 전략을 채택한다.

### 판단 기준

- **confidence_score 0.8 이상**: 확정(CONFIRMED) — 근본 원인 후보로 등록
- **confidence_score 0.3 이하**: 기각(REJECTED) — 트리에서 가지치기
- **confidence_score 0.3~0.8**: 추가조사 필요(NEEDS_INVESTIGATION) — 하위 가설 분기(F11)로 전달

### 핵심 결정사항

1. **Strands SDK structured output + score 기반 재분류**: `ValidationOutput` Pydantic 모델을 `structured_output_model`로 지정하여 LLM이 `status`, `confidence_score`, `reasoning`, `evidence_summary`를 반환한다. LLM이 반환한 status는 참고만 하고, **confidence_score를 기준으로 코드에서 status를 재분류**한다(≥0.8 → CONFIRMED, ≤0.3 → REJECTED, 나머지 → NEEDS_INVESTIGATION). 이는 LLM의 status 판단 일관성 부족을 보완한다.

2. **증거 종합**: 가설별 `evidence_map`에서 증거 텍스트를 가져와 LLM 프롬프트에 포함한다. 증거 수집 모듈은 별도 구현 예정이다.

3. **판단 근거 기록**: LLM의 `reasoning`과 `evidence_summary`를 `ValidationJudgment`에 기록하여 보고서 생성과 사후 검토에 활용한다.

4. **전체 기각 시 가설 재생성**: 오케스트레이션 레이어(`main.py`)에서 `all_rejected` 플래그를 감지하여 가설 생성 단계로 루프백한다. 최대 2회 추가 생성(`RCA_MAX_REGENERATION_ROUNDS`)으로 제한한다.

5. **타임아웃 및 fallback**: 개별 가설 검증에 `ThreadPoolExecutor` 120초 타임아웃을 적용하며, 실패 시 기존 confidence_score를 유지하고 `NEEDS_INVESTIGATION` 상태로 처리하여 추가 조사 기회를 보존한다.

6. **루프 종료 시 미검증 가설 자동 정리 (CLOSED)**: 검증 루프가 종료되면(CONFIRMED 발견, 시간 초과, 최대 루프 등) PENDING 또는 NEEDS_INVESTIGATION 상태로 남은 가설을 **CLOSED**로 처리한다. REJECTED는 증거에 의해 명시적으로 기각된 경우에만 사용하고, 예산 소진/미검증으로 종료된 가설은 CLOSED로 구분한다. `judgment_reasoning`에 종료 사유별 한글 메시지를 기록한다(예: "시간 예산 소진", "확정된 근본원인 발견으로 추가 검증 불필요", "최대 검증 루프 초과" 등). best_hypothesis로 선택된 가설은 제외한다. 이를 통해 세션 완료 시 모든 가설이 CONFIRMED, REJECTED, CLOSED 중 하나의 최종 상태를 갖게 된다. CC Headless에서도 산출물 파싱과 프롬프트로 동일한 동작을 구현한다.

7. **모델 티어**: **Execution 티어**(Haiku 4.5)를 사용한다. 수집된 증거 대비 가설의 지지/반박을 판정하는 단순 분류 작업이므로 경량 모델로 충분하다. [ADR agent/0010](0010-model-tier-architecture.md) 참조.

## Consequences

### Positive

- 복합적 증거를 종합한 유연한 판단으로 기계적 규칙보다 정확한 가설 검증
- 3단 판단(확정/기각/추가조사)으로 불확실한 가설에 대한 점진적 탐색 가능
- 판단 근거 기록으로 보고서의 추론 경로 투명성 확보

### Negative

- LLM 판단의 일관성이 보장되지 않아 동일 증거에 다른 결과가 나올 수 있음
- 신뢰도 임계치(0.3, 0.8)가 장애 유형에 따라 최적이 아닐 수 있음

### Risks

- 증거가 부족한 상태에서 LLM이 높은 신뢰도로 오판할 수 있다. 증거 수량이 최소 기준 미달 시 `NEEDS_INVESTIGATION`으로 강제 처리하여 완화한다.
- 모든 가설 기각 후 추가 가설 생성 루프가 무한 반복될 수 있다. 최대 2회 추가 생성으로 제한한다.

## Related

- [ADR agent/0003: 가설 우선순위 결정](0003-hypothesis-prioritization.md) — 검증 순서를 결정하는 이전 단계
