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
  "review_gate": {
    "early_exit": false,
    "expansion_blocked": true,
    "accepted_max_confidence": 0.85,
    "reason": "expansion_blocked:0.85",
    "auto_rejected_ids": ["hypotheses.json의 UUID(유사도로 자동 기각된 항목)"]
  },
  "summary": "검증 루프 1 완료",
  "output_summary": "채택 1, 기각 2, 조사필요 1, gate=expansion_blocked"
}
```

**review_gate 블록 규칙**:
- `early_exit`: 이 루프에서 Accepted Review Gate가 조기 종료를 요청했는지 여부. `true`이면 메인 에이전트는 다음 루프를 스폰하지 않고 보고서 생성으로 진행한다.
- `expansion_blocked`: 채택 가설 최고 신뢰도가 0.8-0.9 범위여서 **새 분기·재생성을 금지**했는지 여부.
- `accepted_max_confidence`: 채택 가설들 중 최고 신뢰도 (채택 없으면 0.0).
- `reason`: `no_accepted` / `accepted_confidence_met:{score}` / `expansion_blocked:{score}` / `grace_loops_exhausted:{score}` 중 하나.
- `auto_rejected_ids`: 채택 가설과 동일 카테고리 + description Jaccard ≥ 0.6으로 자동 기각된 가설 ID 목록. 이 항목들은 `rejected` 배열에도 `reasoning="Review gate: 이미 채택된 ... 동일 원인 영역"`으로 포함한다.

**주의사항:**
- `confirmed`/`rejected`/`closed`/`needs_investigation`의 각 항목에는 반드시 `reasoning` 필드를 포함한다.
- `new_hypotheses`의 각 항목에는 반드시 `description`과 `category`를 포함한다.
- 모든 가설은 `hypotheses.json`에서 이미 생성된 `hypothesis_id`를 참조해야 한다.
