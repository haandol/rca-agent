#### hypotheses.json

첫 생성은 `hypotheses.json`에 저장한다. 서버가 전체 기각 뒤 `REGENERATE`를 반환하면
두 번째와 세 번째 생성 라운드를 각각 `hypotheses-2.json`, `hypotheses-3.json`에
저장한다. 이전 파일을 덮어쓰지 않는다.

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
      "fault_type": "db-leak | high-cpu | high-memory | slow-query | unsupported",
      "category": "DEPLOYMENT | INFRASTRUCTURE | TRAFFIC | DEPENDENCY | CONFIGURATION",
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
- 한 생성 라운드는 루트 가설 3~5개를 포함한다.
- `title`: 대시보드 카드/그래프 노드에 노출. "Healthcare 앱 커넥션 누수" 같은 **명사구**로 간결히. 물음표·마침표 지양.
- `description`: 가설을 세운 근거와 기대하는 검증 증거를 서술형으로 기술.
- `fault_type`: Healthcare reset 허용 목록과 직접 대응하는 구조화 enum. 네 유형에
  해당하지 않으면 반드시 `unsupported`.
- `validation-{N}.json`의 `new_hypotheses`에도 동일하게 `title`, `description`,
  `fault_type`을 채운다.
- 생성 라운드 번호와 어느 validation 뒤에 재생성됐는지는 저장 서버가 기록한다.
