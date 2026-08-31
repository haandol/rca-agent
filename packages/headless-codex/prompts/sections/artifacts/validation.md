#### validation-{N}.json

**중요: 판정의 `hypothesis_id`는 현재까지 저장된 생성 라운드 또는 앞선 validation의
`new_hypotheses`가 만든 UUID와 정확히 일치해야 한다. 판정에서 새 ID를 만들지 않는다.**

```json
{
  "stage": "VALIDATION",
  "loop_index": 1,
  "confirmed": [
    {
      "hypothesis_id": "기존 hypothesis UUID",
      "confidence": 0.95,
      "fault_type": "증거로 독립 판정한 fault_type enum",
      "reasoning": "확정 근거 (한글, 상세히)",
      "evidence_summary": ["구체적 수치·시각·메시지를 포함한 증거"],
      "evidence_collection_failed": false
    }
  ],
  "rejected": [
    {"hypothesis_id": "기존 hypothesis UUID", "confidence": 0.1, "reasoning": "기각 근거 (한글, 상세히)", "evidence_summary": ["반증 증거"], "evidence_collection_failed": false}
  ],
  "needs_investigation": [
    {"hypothesis_id": "기존 hypothesis UUID", "confidence": 0.5, "reasoning": "추가 조사 필요 사유 (한글, 상세히)", "evidence_summary": ["현재까지의 증거"], "evidence_collection_failed": false}
  ],
  "closed": [],
  "new_hypotheses": [
    {
      "hypothesis_id": "새 UUID (기존과 다른 값)",
      "tree_id": "hypotheses.json의 tree_id와 동일",
      "title": "짧은 한 줄 제목 (≤60자, 한글, 필수)",
      "description": "상세 설명 — 부모 가설을 어떻게 좁혔는지 근거 포함 (한글, 필수)",
      "fault_type": "db-leak | high-cpu | high-memory | slow-query | unsupported",
      "category": "DEPLOYMENT | INFRASTRUCTURE | TRAFFIC | DEPENDENCY | CONFIGURATION",
      "confidence_score": 0.5,
      "required_evidence": ["필요한 증거"],
      "status": "PENDING",
      "parent_id": "분기 원본 가설의 hypothesis_id",
      "depth": 1
    }
  ],
  "summary": "검증 루프 1 완료",
  "output_summary": "신뢰도 판정과 증거 요약"
}
```

저장 서버는 신뢰도와 증거를 다시 판정하고 정규화된 산출물을 저장한다. 저장 응답의
`decision.action`은 `CONTINUE`, `REGENERATE`, `REPORT` 중 하나다. 모델이 다음 단계를
결정하지 않고 이 응답을 따른다. `decision.expansion_blocked=true`이면 새 가설을
분기하지 않고 기존 채택 가설의 증거만 보강한다.

**주의사항:**
- `confirmed`/`rejected`/`closed`/`needs_investigation`의 각 항목에는 반드시 `reasoning` 필드를 포함한다.
- `fault_type`은 초기 hypothesis의 힌트와 독립적으로 증거에서 다시 판정한다.
- 모든 판정은 `evidence_collection_failed` boolean을 포함한다. 증거 도구 호출이나
  조회가 실패했으면 `true`이며, 필수 증거가 있는 가설은 이 값이 `true`이거나
  `evidence_summary`가 비면 높은 신뢰도라도 확정되지 않는다.
- `new_hypotheses`의 각 항목에는 반드시 `title`, `description`, `fault_type`,
  `category`를 포함한다.
- 모든 판정은 현재까지 저장된 가설의 `hypothesis_id`를 참조해야 한다.
- validation 번호는 1부터 연속하며 최대 3이다.
