# ADR 0010: 모델 티어 아키텍처 — 단일 모델(Sonnet 4.6) + Planning/Execution 행동 분리

Date: 2026-04-22

## Status

Accepted (2026-04-29 업데이트 — Haiku Execution 티어 제거, 단일 Sonnet 4.6로 통합)

## Context

초기 설계는 비용 효율을 위해 Planning(Sonnet 4.6 + adaptive thinking) / Execution(Haiku 4.5) 2-tier 모델 분리를 채택했다. 그러나 운영 중 다음 문제가 관찰되었다:

1. **Haiku의 MaxTokensReachedException 반복**: Evidence 수집 서브에이전트가 MCP 도구 호출 결과 + structured output을 생성하는 과정에서 토큰 한도에 도달하는 사례가 다수 발생. 가드레일(증거 실패 시 CONFIRMED 금지)이 반복 트리거되어 불필요한 분기 / 재검증 루프 유발.
2. **세부 정보 누락**: Haiku가 생성한 report / playbook 내용이 Sonnet 결과 대비 증거·타임스탬프 누락이 잦음. 특히 CloudTrail 이벤트 연관성 추론에서 차이가 컸다.
3. **운영 일관성**: CC Headless 엔진은 이미 Sonnet 단일 티어로 동작하므로, Strands만 2-tier를 유지하는 것이 비교·운영에 불편.

## Decision

**단일 모델(Sonnet 4.6) + Planning/Execution 행동 분리**로 전환한다. 모델은 하나지만 호출 시 adaptive thinking 유무를 통해 사고 깊이만 차등화한다.

### 모델 티어

| 티어 | 모델 | 용도 | Thinking |
|------|------|------|----------|
| Planning | Sonnet 4.6 (`BEDROCK_MODEL_ID`) | 추론·판단이 필요한 단계 | Adaptive (`THINKING_ENABLED=true`) |
| Execution | Sonnet 4.6 (`BEDROCK_MODEL_ID`, 동일) | 메트릭 수집·증거 판정 | 없음 |

`BEDROCK_HAIKU_MODEL_ID` / `BEDROCK_HAIKU_MAX_TOKENS` 환경변수는 제거했다. 단일 `BEDROCK_MODEL_ID` / `BEDROCK_MAX_TOKENS`(기본 16384)만 사용한다.

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

매핑 자체는 유지한다. Planning/Execution의 의미는 "thinking 유무"로 축약되어 같은 Sonnet이라도 호출 모드가 다르다.

### Adaptive Thinking

- `additional_request_fields={"thinking": {"type": "adaptive"}}` — 모델이 프롬프트 복잡도에 따라 사고량을 자율 조절.
- `THINKING_ENABLED` 환경변수(`true`/`false`, 기본값 `false`)로 피처플래그 토글.

### 핵심 결정사항

1. **팩토리 함수 유지**: `create_planning_model()` / `create_execution_model()` 두 팩토리는 남긴다. 모델 ID가 같아도 adaptive thinking 유무로 호출 특성이 달라지므로 의도 구분에 유용하다.
2. **Haiku 환경변수 삭제**: `BEDROCK_HAIKU_MODEL_ID` / `BEDROCK_HAIKU_MAX_TOKENS` 제거. 향후 모델 다변화가 필요하면 단계별 ID 오버라이드(`SCOPING_MODEL_ID` 등)를 새로 도입한다.
3. **structured_output과 thinking 호환**: Strands SDK는 `structured_output` 재시도 시 `tool_choice`를 강제하면 thinking을 자동 strip한다. 초회 호출에서는 thinking이 정상 작동하며, 재시도에서만 일시 비활성화되므로 실질적 영향은 미미하다.

## Consequences

### Positive

- Evidence 수집·검증 품질이 Sonnet 수준으로 상향. MaxTokensReachedException 및 가드레일 재트리거 루프 감소.
- 모델 운용이 단일화되어 Bedrock 엔드포인트 / 할당량 / 환경변수 관리가 단순해짐.
- CC Headless 엔진과 동일한 모델 티어로 양 엔진 비교·재현 실험 용이.

### Negative

- Scoping/Validation 비용이 증가(Haiku 대비 약 10배). 호출 빈도가 높은 Validation 단계가 주 영향권.
- Adaptive thinking의 토큰 사용량이 예측 불가하여 비용 추정이 어려움. Planning 단계에만 국한되지만 여전히 변동성 존재.

### Risks

- 비용 상승 억제를 위해 Beam Width·루프 수·Review Gate 등 기존 제어 파라미터를 적극 활용한다.
- 특정 단계(Scoping 등)에서 비용 부담이 크면 향후 단계별 모델 오버라이드를 도입해 선택적으로 경량 모델로 되돌릴 수 있다.

## Related

- [ADR agent/0001: 초기 스코핑 + RCA 보고서 유사도 검색](0001-initial-scoping-and-report-similarity.md) — 스코핑은 Execution 티어 사용
- [ADR agent/0002: 가설 트리 라이프사이클 (Accepted Review Gate)](0002-hypothesis-tree-lifecycle.md) — Review Gate가 비용 폭주 방지
