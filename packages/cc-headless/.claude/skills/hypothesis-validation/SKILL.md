---
name: hypothesis-validation
description: RCA 전문 에이전트의 가설 검증 가이드 — 읽기 전용 증거 수집, 신뢰도 평가, 가설 분기, 종료 판단을 수행하고 JSON 산출물로 기록한다.
---

# 가설 검증 서브에이전트

## 서브에이전트 역할

RCA 전문 에이전트는 검증 루프를 최대 3회 수행한다:

1. 우선순위 결정 → 빔 선택 (상위 3개)
2. 증거 수집 (CloudWatch, CloudTrail, GitHub MCP)
3. 가설 검증 (신뢰도 평가)
4. 가설 분기 (NEEDS_INVESTIGATION 시) — **`expansion_blocked=true`이면 이 단계를 건너뛴다**
5. 결과를 validation JSON 스키마로 정리
6. `save_artifact("validation-{N}.json", ...)`로 산출물 저장 (Python watcher가 DDB에 반영)

## 검증 컨텍스트

각 루프에서 유지해야 하는 필드:

- 현재 가설 목록 (모든 상태, description, confidence_score, fault_type 포함)
- 스코핑 결과
- 알람 상세
- 루프 인덱스 (1-based)
- 기각된 가설 description 목록
- **채택된 가설 목록** (있을 시 `{hypothesis_id, description, confidence, category}`)
- **`expansion_blocked` (bool)** — `true`이면 분기·재생성 금지, 증거 보강만 수행
- **Accepted Review Gate 통과 결과** (early_exit/expansion_blocked/auto_rejected_ids)

서브에이전트는 `auto_rejected_ids`에 포함된 가설을 빔 선택에서 제외하고, 최종 validation JSON의 `rejected` 배열에도 해당 reasoning으로 포함한다.

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

### 3. 가설 검증

각 가설의 증거를 평가하여 상태를 결정한다:

| 신뢰도 | 상태 | 행동 |
|--------|------|------|
| ≥ 0.8 | `CONFIRMED` | 루프 즉시 종료 |
| ≤ 0.3 | `REJECTED` | 다음 가설로 이동 |
| 0.3-0.8 | `NEEDS_INVESTIGATION` | 가설 분기 실행 |

각 가설의 상태, 신뢰도, 판단 근거, 증거 요약을 validation JSON의 해당 배열에 포함한다.
CONFIRMED 항목에는 참조 hypothesis와 같은 구조화 `fault_type`을 포함한다.

### 4. 가설 분기

**`expansion_blocked=true`이면 이 단계 전체를 건너뛴다.** 비확장 모드에서는 증거 보강만 허용된다.

NEEDS_INVESTIGATION 가설에 대해:
- **2-3개** 더 구체적인 하위 가설을 생성한다
- 부모보다 구체적이고 검증 가능해야 한다
- 기각된 가설과 중복되지 않아야 한다
- **이미 채택된 가설과 같은 카테고리 + description 유사도(Jaccard) ≥ 0.6인 하위 가설은 생성 금지** (중복 탐색 억제)
- **최대 깊이 3레벨**
- 하위 가설은 validation JSON의 `new_hypotheses` 배열에 포함한다
- 하위 가설에도 `fault_type` enum을 포함한다

## 반환값

서브에이전트는 반드시 다음 JSON 형태로 결과를 반환한다:

```json
{
  "loop_index": 1,
  "judgments": [
    {
      "hypothesis_id": "uuid",
      "status": "CONFIRMED|REJECTED|CLOSED|NEEDS_INVESTIGATION",
      "confidence_score": 0.85,
      "fault_type": "db-leak|high-cpu|high-memory|slow-query|unsupported",
      "reasoning": "판단 근거"
    }
  ],
  "all_rejected": false,
  "new_children_count": 2,
  "confirmed_hypothesis": {
    "hypothesis_id": "uuid",
    "description": "확정된 근본원인",
    "confidence_score": 0.92,
    "fault_type": "db-leak"
  }
}
```

- `confirmed_hypothesis`는 CONFIRMED 가설이 있을 때만 포함
- `all_rejected`가 true이면 메인 에이전트가 재생성을 결정한다

## 산출물 저장

검증 루프를 마치면 `save_artifact("validation-1.json", "<validation JSON>")`을
한 번 호출한다. Python watcher가 파일을 감지해 span과 가설 상태를 DDB에 반영한다.

## 종료 조건

RCA 전문 에이전트는 각 루프 후 다음 조건을 판단한다:
- **CONFIRMED**: 신뢰도 ≥ 0.9인 가설 확정 → 보고서 생성 진입
- **TIMEOUT**: 8분 초과 → 현재 최선 결과로 보고서
- **MAX_LOOPS**: 검증 루프 3회 완료 → 보고서 생성
- **MAX_DEPTH**: 가설 트리 깊이 3 도달 → 분기 중단
- **ALL_REJECTED**: 모든 기각 → 재생성 (최대 2회)
