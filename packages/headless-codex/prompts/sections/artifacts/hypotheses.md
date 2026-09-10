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
- 한 가설은 독립적으로 반증할 수 있는 한 메커니즘이다. 반증 조건이 다른 설정 회귀와
  누수를 "회귀 또는 누수" 한 가설로 묶지 않는다. 루트 가설 수는 3~5개를 유지한다.
- `fault_type`: 원인 분류이며 실행 허용 목록이 아니다. `slow-query`는 비효율 SQL 작업이나
  호출 증폭이며 외부 트랜잭션 잠금에 의한 SQL 대기는 `unsupported`다. 풀 설정 축소를
  반환 누락 증거 없이 `db-leak`로 분류하지 않는다. 기존 다섯 유형 이외의 enum을 만들지 않는다.
- `validation-{N}.json`의 `new_hypotheses`에도 동일하게 `title`, `description`,
  `fault_type`을 채운다.
- 생성 라운드 번호와 어느 validation 뒤에 재생성됐는지는 저장 서버가 기록한다.
