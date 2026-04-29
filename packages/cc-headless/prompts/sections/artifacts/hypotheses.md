#### hypotheses.json

```json
{
  "stage": "HYPOTHESIS_GENERATION",
  "tree_id": "공유 UUID",
  "hypotheses": [
    {
      "hypothesis_id": "UUID",
      "tree_id": "공유 UUID",
      "description": "가설 설명 (한글)",
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
