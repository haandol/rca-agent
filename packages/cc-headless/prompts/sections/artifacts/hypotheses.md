#### hypotheses.json

```json
{
  "stage": "HYPOTHESIS_GENERATION",
  "tree_id": "공유 UUID",
  "hypotheses": [
    {
      "hypothesis_id": "UUID",
      "tree_id": "공유 UUID",
      "title": "짧은 한 줄 제목 (≤60자, 한글, 필수)",
      "description": "상세 설명. 왜 이 가설을 제기하는지 근거와 검증 방향을 2-4문장 (한글)",
      "category": "INFRASTRUCTURE | DEPLOYMENT | TRAFFIC | DEPENDENCY | APPLICATION",
      "confidence_score": 0.6,
      "required_evidence": ["필요한 증거 목록"],
      "status": "PENDING",
      "parent_id": null,
      "depth": 0
    }
  ],
  "summary": "가설 N개 생성",
  "output_summary": "가설 5개 생성: 커넥션 누수, CPU 스트레스, ..."
}
```

**필드 규칙**:
- `title`: 대시보드 카드/그래프 노드에 노출. "Healthcare 앱 커넥션 누수" 같은 **명사구**로 간결히. 물음표·마침표 지양.
- `description`: 가설을 세운 근거와 기대하는 검증 증거를 서술형으로 기술.
- `validation-{N}.json`의 `new_hypotheses`에도 동일하게 `title`·`description` 쌍을 채운다.
