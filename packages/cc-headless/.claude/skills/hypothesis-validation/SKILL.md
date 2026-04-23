---
name: hypothesis-validation
description: 가설 검증 서브에이전트 가이드 — 증거 수집, 신뢰도 평가, 가설 분기, 종료 판단을 수행하고 rca-progress MCP로 결과를 기록한다. Agent tool로 가설 검증 서브에이전트를 스폰할 때 이 스킬을 따른다.
---

# 가설 검증 서브에이전트

## 서브에이전트 역할

메인 에이전트가 Agent tool로 이 서브에이전트를 스폰한다. 서브에이전트는 **검증 루프 1회**를 수행한다:

1. 우선순위 결정 → 빔 선택 (상위 3개)
2. 증거 수집 (CloudWatch, CloudTrail, GitHub MCP)
3. 가설 검증 (신뢰도 평가)
4. 가설 분기 (NEEDS_INVESTIGATION 시)
5. 결과를 rca-progress MCP로 DDB에 반영
6. `save_artifact("validation-{N}.md", ...)`로 산출물 저장 (자동으로 `/tmp/rca-{세션ID}/` 아래에 저장됨)

## 검증 루프 절차

### 1. 우선순위 결정

PENDING/NEEDS_INVESTIGATION 상태의 가설을 우선순위로 정렬한다:
- 높은 신뢰도 우선
- 동률 시 카테고리 순서: DEPLOYMENT > INFRASTRUCTURE > TRAFFIC > DEPENDENCY > CONFIGURATION
- 상위 **3개**를 빔으로 선택한다 (beam width)

### 2. 증거 수집

빔에 포함된 각 가설에 대해:
- **메트릭 분석**: CloudWatch MCP로 알람 메트릭 + 관련 메트릭 조회 (알람 전후 1시간)
- **로그 분석**: CloudWatch Logs Insights로 ERROR/WARN/Exception/timeout 패턴 검색
- **변경 상관**: CloudTrail로 알람 전 1시간 이내 배포·설정 변경 이벤트 조회

수집된 증거마다 **구체적 데이터 포인트, 타임스탬프, 에러 메시지**를 포함한다.

증거 수집 후 `report_progress("EVIDENCE_COLLECTION", summary)`를 호출한다.

### 3. 가설 검증

각 가설의 증거를 평가하여 상태를 결정한다:

| 신뢰도 | 상태 | 행동 |
|--------|------|------|
| ≥ 0.8 | `CONFIRMED` | 루프 즉시 종료 |
| ≤ 0.3 | `REJECTED` | 다음 가설로 이동 |
| 0.3-0.8 | `NEEDS_INVESTIGATION` | 가설 분기 실행 |

`update_hypothesis`로 각 가설의 상태, 신뢰도, 판단 근거, 증거 요약을 DDB에 반영한다.
`report_progress("HYPOTHESIS_VALIDATION", summary)`를 호출한다.

### 4. 가설 분기

NEEDS_INVESTIGATION 가설에 대해:
- **2-3개** 더 구체적인 하위 가설을 생성한다
- 부모보다 구체적이고 검증 가능해야 한다
- 기각된 가설과 중복되지 않아야 한다
- **최대 깊이 3레벨**
- `save_hypotheses`로 하위 가설을 DDB에 저장한다

## 반환값

서브에이전트는 반드시 다음 JSON 형태로 결과를 반환한다:

```json
{
  "loop_index": 1,
  "judgments": [
    {
      "hypothesis_id": "uuid",
      "status": "CONFIRMED|REJECTED|NEEDS_INVESTIGATION",
      "confidence_score": 0.85,
      "reasoning": "판단 근거"
    }
  ],
  "all_rejected": false,
  "new_children_count": 2,
  "confirmed_hypothesis": {
    "hypothesis_id": "uuid",
    "description": "확정된 근본원인",
    "confidence_score": 0.92
  }
}
```

- `confirmed_hypothesis`는 CONFIRMED 가설이 있을 때만 포함
- `all_rejected`가 true이면 메인 에이전트가 재생성을 결정한다

## MCP 호출 순서

1. `report_progress("HYPOTHESIS_PRIORITIZATION", "가설 3개 우선순위 결정")`
2. `report_progress("EVIDENCE_COLLECTION", "빔 3개 가설에 대해 메트릭/로그/변경이력 수집")`
3. 각 가설에 `update_hypothesis(...)` 호출
4. `report_progress("HYPOTHESIS_VALIDATION", "CONFIRMED 1개, REJECTED 2개")`
5. `save_artifact("validation-1.md", "# 검증 루프 1\n\n...")`

## 종료 조건 (메인 에이전트가 판단)

서브에이전트는 루프 1회만 수행하고 결과를 반환한다. 다음 조건은 메인 에이전트가 판단:
- **CONFIRMED**: 신뢰도 ≥ 0.9인 가설 확정 → 보고서 생성 진입
- **TIMEOUT**: 8분 초과 → 현재 최선 결과로 보고서
- **MAX_LOOPS**: 검증 루프 3회 완료 → 보고서 생성
- **MAX_DEPTH**: 가설 트리 깊이 3 도달 → 분기 중단
- **ALL_REJECTED**: 모든 기각 → 재생성 (최대 2회)
