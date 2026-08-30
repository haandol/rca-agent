## 산출물 규칙

각 호출은 빈 실행별 산출물 디렉터리에서 시작하는 독립 RCA이다. 이전 호출의
산출물을 재사용하지 않는다. 경로를 직접 읽거나 조작하지 않는다.

| 파일명                | 작성 주체 | 저장 방법                |
| --------------------- | --------- | ------------------------ |
| `scoping.json`        | RCA       | `save_analysis_artifact` |
| `hypotheses.json`     | RCA       | `save_analysis_artifact` |
| `validation-{N}.json` | RCA       | `save_analysis_artifact` |
| `report.md`           | Report    | `save_report_artifact`   |
| `playbook.json`       | Report    | `save_report_artifact`   |

저장 도구는 역할별로 갈라져 있고 각 역할은 자기 도구만 갖는다. 다른 역할의 산출물을
저장하려 해도 도구가 없어 저장되지 않으므로, 작성 주체는 권고가 아니라 경계다.

`report.md`와 `playbook.json`은 **하나의 리포트를 이루는 두 표현**이다. 사람이 읽는
서술은 `report.md`가, 실행과 유사도 검색이 쓰는 구조는 `playbook.json`이 담는다. 두
표현의 절차가 어긋나면 사용자가 승인한 것과 실행되는 것이 달라지므로 저장이 거부된다.

Python wrapper는 산출물을 감시해 세션 트레이스를 기록한다. JSON 산출물은 반드시
valid object여야 한다.
