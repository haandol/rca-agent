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
4. 가설 분기 (서버 응답의 `expansion_blocked=true`이면 건너뛴다)
5. 결과를 validation JSON 스키마로 정리
6. `save_analysis_artifact("validation-{N}.json", ...)`로 산출물을 저장하고 서버의 `decision`을 따른다

## 검증 컨텍스트

각 루프에서 유지해야 하는 필드:

- 현재 가설 목록 (모든 상태, description, confidence_score, fault_type 포함)
- 스코핑 결과
- 알람 상세
- 루프 인덱스 (1-based)
- 기각된 가설 description 목록
- 이전 validation 저장 응답의 서버 `decision`

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
| ≥ 0.8 | `CONFIRMED` 후보 | 서버가 종료·비확장 여부를 판정 |
| ≤ 0.3 | `REJECTED` | 다음 가설로 이동 |
| 0.3-0.8 | `NEEDS_INVESTIGATION` | 가설 분기 실행 |

각 가설의 상태 후보, 신뢰도, 판단 근거, 증거 요약을 validation JSON에 포함한다.
서버가 신뢰도로 상태를 다시 분류한다. CONFIRMED 후보의 `fault_type`은 초기 힌트를
복사하지 말고 실제 증거를 근거로 독립 판정한다.

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

## validation JSON 스키마

판정은 상태별 배열로 분리해 기록한다. 서버는 버킷 이름을 신뢰하지 않고 confidence와
증거 요약으로 정규화한다.

**다섯 배열은 항목이 없어도 빈 배열로 반드시 포함한다.** 누락하면 리포트를 다
만들어도 세션이 완료되지 않는다.

```json
{
  "stage": "VALIDATION",
  "loop_index": 1,
  "summary": "이번 루프에서 무엇을 검증했는지",
  "output_summary": "상위 단계에 전달할 요약",
  "confirmed": [
    {
      "hypothesis_id": "기존 가설의 uuid",
      "confidence": 0.92,
      "fault_type": "db-leak",
      "reasoning": "판단 근거",
      "evidence_summary": ["증거 수치와 시각"],
      "evidence_collection_failed": false
    }
  ],
  "rejected": [{ "hypothesis_id": "uuid", "confidence": 0.1, "reasoning": "...", "evidence_summary": ["반증"], "evidence_collection_failed": false }],
  "needs_investigation": [{ "hypothesis_id": "uuid", "confidence": 0.5, "reasoning": "...", "evidence_summary": ["현재 증거"], "evidence_collection_failed": false }],
  "closed": [],
  "new_hypotheses": [
    {
      "hypothesis_id": "새 uuid",
      "tree_id": "부모 hypothesis와 같은 tree_id",
      "parent_id": "분기 대상 가설의 uuid",
      "title": "제목",
      "description": "부모보다 구체적인 서술",
      "category": "카테고리",
      "status": "PENDING",
      "fault_type": "db-leak",
      "confidence_score": 0.5,
      "depth": 2,
      "required_evidence": ["수집할 증거"]
    }
  ]
}
```

배열 항목의 제약:

- `stage`는 `VALIDATION`이고 `loop_index`는 **파일명의 N과 같아야** 한다
  (`validation-2.json` → `loop_index: 2`).
- 네 판정 배열의 각 항목은 `hypothesis_id`·`reasoning`·`confidence`를 갖는다.
  `confidence`는 0~1이며 필드 이름은 `confidence_score`가 아니다.
- **같은 `hypothesis_id`를 두 배열에 넣지 않는다.** 한 가설은 한 상태만 갖는다.
- `confirmed` 후보는 증거에서 독립 판정한 `fault_type`을 갖는다.
- 모든 판정 항목은 `evidence_summary` 배열과 `evidence_collection_failed` boolean을
  갖는다. 증거 도구 호출이나 조회가 실패했으면 `true`로 기록한다. 필수 증거가 있는
  가설은 실패 값이 `true`이거나 증거 요약이 비면 확정될 수 없다.
- `closed`는 서버 소유 배열이므로 모델은 빈 배열을 제출한다.
- `new_hypotheses`는 `confidence_score`(0~1)를 쓰고, `depth`는 **부모 depth + 1**,
  `tree_id`는 부모 가설의 값과 같아야 하며 `parent_id`는 기존 가설을
  가리켜야 한다. `hypothesis_id`는 기존·신규 전체에서 유일해야 한다.

가설이 전부 기각됐는지와 재생성 가능 여부는 저장 서버가 결정한다.

## 산출물 저장

검증 루프를 마치면 연속 번호로 저장한다. 저장 응답이 `ok: false`이면 내용을 고쳐
같은 번호로 다시 저장한다. 성공 응답의 `decision.action`을 반드시 따른다.

## 종료 조건

서버는 다음 조건을 판정한다:
- 신뢰도 0.9 이상 또는 비확장 grace 2회 → `REPORT`
- validation 3회 완료 → `REPORT`
- depth 3에서 더 분기할 수 없음 → `REPORT`
- 전체 기각이고 재생성 2회 미만 → `REGENERATE`
- 그 외 → `CONTINUE`

전체 프로세스 시간 상한은 Python wrapper가 관리한다. 에이전트가 별도의 8분
타이머를 추정해 조기 종료하지 않는다.
