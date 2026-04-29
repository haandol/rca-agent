#### validation-{N}.json

**중요: confirmed/rejected/closed/needs_investigation의 `hypothesis_id`는 반드시 `hypotheses.json`에서 생성한 UUID와 정확히 일치해야 한다. 새로운 ID를 만들지 않는다.**

```json
{
  "stage": "VALIDATION",
  "loop_index": 1,
  "confirmed": [
    {"hypothesis_id": "hypotheses.json의 UUID", "confidence": 0.95, "reasoning": "확정 근거 (한글, 상세히)"}
  ],
  "rejected": [
    {"hypothesis_id": "hypotheses.json의 UUID", "confidence": 0.1, "reasoning": "기각 근거 (한글, 상세히)"}
  ],
  "needs_investigation": [
    {"hypothesis_id": "hypotheses.json의 UUID", "confidence": 0.5, "reasoning": "추가 조사 필요 사유 (한글, 상세히)"}
  ],
  "closed": [
    {"hypothesis_id": "hypotheses.json의 UUID", "confidence": 0.4, "reasoning": "종료 사유 (예: 시간 예산 소진, 확정된 근본원인 발견으로 추가 검증 불필요)"}
  ],
  "new_hypotheses": [
    {
      "hypothesis_id": "새 UUID (기존과 다른 값)",
      "tree_id": "hypotheses.json의 tree_id와 동일",
      "description": "새 가설 설명 (한글, 필수)",
      "category": "INFRASTRUCTURE | DEPLOYMENT | TRAFFIC | DEPENDENCY | APPLICATION",
      "confidence_score": 0.5,
      "required_evidence": ["필요한 증거"],
      "status": "PENDING",
      "parent_id": "분기 원본 가설의 hypothesis_id",
      "depth": 1
    }
  ],
  "best_hypothesis": {"hypothesis_id": "hypotheses.json의 UUID", "confidence": 0.95},
  "summary": "검증 루프 1 완료",
  "output_summary": "확정 1, 기각 2, 조사필요 1"
}
```

**주의사항:**
- `confirmed`/`rejected`/`closed`/`needs_investigation`의 각 항목에는 반드시 `reasoning` 필드를 포함한다.
- `new_hypotheses`의 각 항목에는 반드시 `description`과 `category`를 포함한다.
- 모든 가설은 `hypotheses.json`에서 이미 생성된 `hypothesis_id`를 참조해야 한다.
