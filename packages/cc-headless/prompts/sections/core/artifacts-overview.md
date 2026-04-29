## 산출물 규칙

모든 중간 산출물은 **JSON** 파일로 `/tmp/rca-{RCA_ID}/`에 저장한다. `save_artifact(filename, content)`를 사용한다.

Python wrapper가 이 파일들을 감시하여 대시보드 트레이스를 자동 생성한다. **파일이 생성되는 순간 해당 단계가 완료된 것으로 기록되므로, 반드시 각 단계 완료 후 즉시 산출물을 저장한다.**

| 파일명 | 단계 | 형식 |
|--------|------|------|
| `scoping.json` | 초기 스코핑 | JSON |
| `hypotheses.json` | 가설 생성 | JSON |
| `validation-{N}.json` | N번째 검증 루프 | JSON |
| `playbook.json` | 플레이북 | JSON |
| `report.md` | 최종 보고서 | Markdown |
