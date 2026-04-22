# ADR 0010: 모델 티어 아키텍처 — 계획/실행/오케스트레이션 모델 분리

Date: 2026-04-22

## Status

Accepted

## Context

모든 파이프라인 단계가 동일한 Sonnet 4.6 모델을 사용하면 비용 효율이 낮고, 단순 메트릭 수집이나 증거 판정 같은 경량 작업에 고비용 모델을 낭비하게 된다. 반면 가설 생성, 우선순위 결정, 보고서 작성 등 추론이 필요한 단계에서는 adaptive thinking이 품질을 크게 향상시킨다.

## Decision

**3-tier 모델 아키텍처**를 채택한다.

### 모델 티어

| 티어 | 모델 | 용도 | Thinking |
|------|------|------|----------|
| Planning | Sonnet 4.6 (`BEDROCK_MODEL_ID`) | 추론·판단이 필요한 단계 | Adaptive |
| Execution | Haiku 4.5 (`BEDROCK_HAIKU_MODEL_ID`) | 메트릭 수집·증거 판정 | 없음 |

### 파이프라인 단계 → 모델 매핑

| 단계 | 티어 | 근거 |
|------|------|------|
| F1 Scoping | Execution | CloudWatch MCP 도구 호출 + 얕은 분석 |
| F2 Hypothesis Generation | Planning | 다각도 근본 원인 추론 |
| F3 Prioritization | Planning | 가설 간 상대적 중요도 판단 |
| F4 Validation | Execution | 수집된 증거 대비 단순 판정 |
| F5 Branching | Planning | 하위 가설 도출 추론 |
| F7 Report Generation | Planning | 구조화된 보고서 작성 |
| F8 Playbook Generation | Planning | 장애 패턴 추출 및 절차 작성 |

### Adaptive Thinking

- `additional_request_fields={"thinking": {"type": "adaptive"}}` — 모델이 프롬프트 복잡도에 따라 사고량을 자율 조절한다. `budget_tokens` 지정이 불필요하다.
- `THINKING_ENABLED` 환경변수(`true`/`false`, 기본값 `true`)로 피처플래그 토글 가능하다. `false`로 설정 시 모든 Planning 티어 에이전트에서 thinking이 비활성화되어 Sonnet 4.6 기본 모드로 동작한다.

### 핵심 결정사항

1. **팩토리 함수 분리**: `create_planning_model()` (Sonnet + adaptive thinking)과 `create_execution_model()` (Haiku, thinking 없음) 두 팩토리로 분리한다.

2. **피처플래그**: `THINKING_ENABLED` 환경변수로 adaptive thinking을 전역적으로 끄고 켤 수 있다. 비용 제어, A/B 테스트, 장애 시 빠른 비활성화에 활용한다.

3. **모델 ID 오버라이드**: `BEDROCK_MODEL_ID`, `BEDROCK_HAIKU_MODEL_ID` 환경변수로 각 티어의 모델을 교체할 수 있다.

4. **structured_output과 thinking 호환**: Strands SDK는 `structured_output` 재시도 시 `tool_choice`를 강제하면 thinking을 자동 strip한다. 초회 호출에서는 thinking이 정상 작동하며, 재시도에서만 일시 비활성화되므로 실질적 영향은 미미하다.

## Consequences

### Positive

- Haiku 사용으로 Scoping/Validation 단계의 비용이 약 1/10로 절감
- Adaptive thinking으로 Planning 단계의 추론 품질 향상 (가설 다양성, 보고서 깊이)
- 피처플래그로 thinking을 즉시 비활성화할 수 있어 비용 폭증 시 빠른 대응 가능
- 모델 교체가 환경변수 수준에서 가능하여 모델 업그레이드에 유연

### Negative

- Haiku의 추론 능력이 Sonnet보다 낮아 복잡한 증거 판정에서 정확도 하락 가능
- 모델 2종 운용으로 Bedrock 엔드포인트 관리 복잡도 증가
- Adaptive thinking의 토큰 사용량이 예측 불가하여 비용 추정이 어려움

### Risks

- Haiku가 Scoping에서 MCP 도구를 적절히 호출하지 못할 경우, 환경변수로 Sonnet으로 전환하여 완화한다.

## Related

- [ADR agent/0001: 초기 스코핑 전략](0001-initial-scoping-strategy.md) — Execution 티어 사용
- [ADR agent/0002: 가설 생성](0002-hypothesis-generation.md) — Planning 티어 사용
- [ADR agent/0004: 가설 검증](0004-hypothesis-validation-pruning.md) — Execution 티어 사용
