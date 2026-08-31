# ADR 0010: 실행 엔진별 모델과 추론 강도 고정

Date: 2026-04-22
Updated: 2026-08-30

## Status

Accepted (2026-08-30)

## Context

RCA 시스템은 Strands 파이프라인과 Headless Codex 오케스트레이터를 독립 실행한다.
두 엔진은 오케스트레이션과 모델 런타임이 다르므로, 평가는 같은 RCA 산출물 계약을
충족하는 두 시스템을 비교한다.

Strands 파이프라인 안에서도 단계별 추론 깊이는 다르다. 가설 생성·우선순위 결정·
보고서 작성은 다각도 추론이 필요하고, 메트릭 조회와 증거-가설 일치 판정은 수집된
데이터에 대한 분류에 가깝다. 단계별로 경량 모델을 섞으면 MCP 도구 응답과 구조화
출력을 함께 생성하는 과정에서 증거·타임스탬프 누락이 늘어난다. 따라서 Strands는
한 모델 세대를 유지하되 단계별 사고 동작만 나눈다.

Headless Codex는 분석, 승인된 플레이북 실행, 회고를 하나의 Codex 런타임 계열로
운영한다. 이 경로는 도구 사용과 다단계 위임의 품질을 우선하며, 실행마다 모델이나
추론 강도가 달라지면 같은 이미지와 하네스를 재현할 수 없다.

## Decision Drivers

- 증거 수집 단계는 도구 응답과 구조화 출력을 함께 생성해도 토큰 한도에 걸리지
  않아야 한다.
- 보고서·플레이북에 증거와 타임스탬프가 누락되지 않아야 한다.
- Strands 안에서는 단계별 추론 깊이 차이를 유지해 불필요한 사고 비용을 줄여야 한다.
- Headless Codex의 분석·실행·회고는 같은 모델과 추론 강도로 재현 가능해야 한다.
- 모델 호출은 태스크 역할의 AWS 자격 증명 경계 안에서 이루어져야 한다.
- 요청한 모델이나 추론 프로필을 사용할 수 없을 때 다른 모델로 대체해서는 안 된다.

## Decision

실행 엔진별 모델과 추론 강도를 다음처럼 고정한다.

| 실행 엔진 | 모델 | 추론 계약 |
|------|------|----------|
| Strands | Claude Sonnet 5 세대 | Planning은 adaptive thinking, Execution은 thinking 없음 |
| Headless Codex | Bedrock Global Inference Profile `global.openai.gpt-5.6-sol` | 분석·실행·회고 모두 reasoning effort `high` |

Headless Codex는 Amazon Bedrock Runtime의 OpenAI Responses 호환 경로를 사용한다.
인증 경계는 ECS 태스크 역할이며 OpenAI API 키나 장기 정적 토큰을 배포하지 않는다.
요청한 Global Inference Profile을 호출할 수 없으면 실행을 실패시키고 기존
SQS 재전달·DLQ 정책에 맡긴다. 다른 모델로 자동 대체하지 않는다.

### Strands 티어의 의미

| 티어 | 용도 | Thinking |
|------|------|----------|
| Planning | 추론·판단이 필요한 단계 | Adaptive |
| Execution | 도구 호출·증거 판정 | 없음 |

Adaptive thinking은 사고량을 모델이 프롬프트 복잡도에 따라 자율 조절하는 모드다.
Strands의 기본값은 비활성이며 비용 관측이 확보된 환경에서만 Planning 경로에 켠다.

Strands의 Sonnet 5 호출 표면에는 두 가지 제약이 있다.

1. **sampling 파라미터를 전달하지 않는다.** `temperature`는 이 세대에서 거부되어
   요청 자체가 실패한다. 출력 특성은 프롬프트로 조정한다.
2. **사고량은 adaptive 여부로만 제어하고 effort 수준을 전달하지 않는다.** effort는
   현재 대상 플랫폼 배포에서 허용되지 않는다. Planning/Execution 구분은 adaptive
   활성 여부만으로 표현한다.

Headless Codex는 sampling 파라미터를 별도로 고정하지 않고 Codex 런타임의
Responses 요청 계약을 따른다. reasoning effort는 `high`를 명시해 기본값 변화가
실행 품질과 지연을 바꾸지 못하게 한다.

### Strands 파이프라인 단계 → 티어 매핑

| 단계 | 티어 | 근거 |
|------|------|------|
| Scoping | Execution | 도구 호출 + 얕은 분석 |
| Hypothesis Generation | Planning | 다각도 근본 원인 추론 |
| Prioritization | Planning | 가설 간 상대적 중요도 판단 |
| Validation | Execution | 수집된 증거 대비 판정 |
| Branching | Planning | 하위 가설 도출 추론 |
| Report Generation | Planning | 구조화된 보고서 작성 |
| Playbook Generation | Planning | 장애 패턴 추출 및 절차 작성 |

### 핵심 결정사항

1. **Strands 티어 구분을 코드 경계로 유지**: 모델 ID가 같아도 Planning/Execution을 별도
   생성 경로로 남긴다. 호출 특성(thinking 유무)이 다르고, 향후 단계별 모델
   오버라이드가 필요해지면 이 경계가 그 진입점이 된다.
2. **Strands 단계별 모델 오버라이드는 도입하지 않는다**: 단일 모델 ID만 설정으로
   노출한다. 특정 단계의 비용 부담이 실측으로 확인되면 그때 단계별 오버라이드를
   추가한다. 예상만으로 설정 표면을 넓히지 않는다.
3. **structured output 재시도 시 thinking 손실을 수용한다**: SDK가 구조화 출력
   재시도에서 도구 선택을 강제하면 thinking이 제거된다. 초회 호출에서는 정상
   작동하므로 재시도 경로에만 영향이 있고, 이를 우회하려 재시도 자체를 없애면
   구조화 출력 실패가 파이프라인 실패로 직결된다.
4. **허용되지 않는 파라미터는 코드에서 제거한다**: 플랫폼이 거부하는 파라미터를
   기본값으로 남겨 두면 모든 호출이 실패한다. 프롬프트로 대체 가능한 제어는 프롬프트로
   옮기고, 파라미터 집합의 일치를 테스트로 고정한다.
5. **Codex 모델 패리티를 검증한다**: 실모델 평가와 배포 태스크는 모델 ID
   `global.openai.gpt-5.6-sol`과 reasoning effort `high`가 모두 일치해야 한다.
6. **Codex 경로에 모델 fallback을 두지 않는다**: 정확한 모델·추론 계약을 만족하지
   못한 실행은 품질 비교 자료나 배포 성공으로 인정하지 않는다.

## 대안 검토

| 대안 | 장점 | 단점 및 미채택 이유 |
|------|------|---------------------|
| Strands 단계별로 서로 다른 모델(경량/고성능 2-tier) | 호출 빈도가 높은 단계의 비용을 크게 낮춘다. | 경량 모델이 도구 응답 + 구조화 출력에서 토큰 한도에 걸리고 증거를 누락한다. 확정 금지 가드레일이 반복 트리거되어 절감이 루프 비용으로 상쇄된다. |
| 모든 단계에 동일 모델 + 항상 thinking | 품질이 가장 균일하다. | 단순 분류 단계까지 사고 토큰을 소모하고 사용량 예측이 어려워진다. |
| Strands 단일 모델 + thinking 유무로 행동 분리 | 품질을 확보하면서 단계별 사고 깊이를 차등화한다. | 경량 모델 대비 조회·판정 단계 비용이 오르므로 탐색 폭 제어에 더 의존한다. |
| Codex가 리전별 foundation model을 직접 호출 | 특정 리전에 고정할 수 있다. | 사용자가 요구한 전역 추론 프로필 계약을 충족하지 않고 리전별 용량 차이를 애플리케이션이 떠안는다. |
| Codex 모델 오류 시 다른 모델로 fallback | 일시적 모델 장애에도 실행 성공률이 높아진다. | 실행마다 모델과 품질 기준이 달라져 배포·평가 패리티가 무너진다. |

## Consequences

### Positive

- 증거 수집·검증 품질이 균일해지고 토큰 한도 초과로 인한 가드레일 재트리거 루프가
  사라진다.
- 모델 엔드포인트·할당량·설정이 단일화되어 운용이 단순해진다.
- 각 엔진의 모델과 추론 강도가 명시되어 배포·실모델 평가 결과를 재현할 수 있다.
- Headless Codex는 전역 용량 라우팅을 사용하면서 태스크 역할 밖의 장기 비밀을 요구하지 않는다.

### Negative

- 조회·판정 단계 비용이 경량 모델 대비 상승한다. 호출 빈도가 높은 검증 단계가 주
  영향권이다.
- Adaptive thinking의 토큰 사용량이 예측 불가하여 비용 추정이 어렵다. Planning
  단계에 국한되지만 변동성은 남는다.
- sampling 파라미터를 쓸 수 없으므로 출력 편차 제어가 프롬프트에만 의존한다.
- effort를 쓸 수 없어 Planning/Execution 사이의 사고 깊이 차등이 adaptive 온·오프
  두 단계로만 표현된다.
- 두 엔진이 다른 모델 계열을 사용하므로 결과 차이를 오케스트레이션 차이만으로
  해석할 수 없다. 평가는 시스템 전체의 계약 충족 여부를 비교해야 한다.
- Headless Codex가 `high` 추론을 고정하므로 낮은 effort보다 지연과 비용이 증가할 수 있다.

### Risks

- 플랫폼의 파라미터 허용 집합이 배포마다 달라질 수 있다. 문서만 근거로 파라미터를
  추가하면 전체 호출이 실패하므로, 모델·플랫폼을 바꿀 때 실측 확인을 선행한다.

- 비용 상승이 탐색 제어에 의존한다. Beam width, 검증 루프 한도, Accepted Review
  Gate가 실질적인 비용 상한 역할을 하므로 이 값들을 느슨하게 바꾸면 단일 모델
  전환의 비용 영향이 증폭된다([ADR 0002](0002-hypothesis-tree-lifecycle.md),
  [ADR 0006](0006-termination-conditions.md)).
- 특정 단계의 비용 부담이 실측으로 확인되면 단계별 모델 오버라이드가 필요해진다.
  티어 경계를 코드에 유지하는 이유가 이 확장 경로다.
- Bedrock Global Inference Profile 또는 Responses 호환 경로가 일시적으로
  불가하면 Codex 실행이 실패한다. 모델 fallback을 두지 않으므로 SQS 재전달과
  DLQ가 가용성 복구 경로다.

## Related

- [ADR agent/0001: 초기 스코핑 + RCA 보고서 유사도 검색](0001-initial-scoping-and-report-similarity.md) — 스코핑은 Execution 티어 사용
- [ADR agent/0002: 가설 트리 라이프사이클](0002-hypothesis-tree-lifecycle.md) — 탐색 제어 파라미터가 비용 상한 역할
- [ADR agent/0015: Hexagonal Architecture](0015-hexagonal-architecture.md) — 모델 생성 경로가 DI Container를 통해 주입됨
