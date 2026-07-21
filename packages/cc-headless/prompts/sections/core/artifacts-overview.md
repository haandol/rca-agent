## 산출물 규칙

각 호출은 빈 실행별 산출물 디렉터리에서 시작하는 독립 RCA이다. 이전 호출의
산출물을 재사용하지 않는다. 경로를 직접 읽거나 조작하지 않는다.

| 파일명 | 작성 주체 | 저장 방법 |
|--------|----------|----------|
| `scoping.json` | RCA | `save_artifact` |
| `hypotheses.json` | RCA | `save_artifact` |
| `validation-{N}.json` | RCA | `save_artifact` |
| `remediation.json` | narrow remediation MCP | 서버가 자동 저장 |
| `report.md` | Report | `save_artifact` |
| `playbook.json` | Report | `save_artifact` |

Python wrapper는 산출물을 감시해 세션 트레이스를 기록한다. JSON 산출물은 반드시
valid object여야 한다.
