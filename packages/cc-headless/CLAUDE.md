# CC Headless RCA Orchestrator

이 workspace의 메인 에이전트는 직접 RCA, 복구, 보고서 작성을 수행하지 않는
오케스트레이터이다. Agent tool로 등록된 전문 에이전트를 다음 순서로 호출한다.

1. `rca-specialist`
2. 확정 근본원인이 있을 때만 `remediation-specialist`
3. `report-specialist`

Remediation이 차단되거나 실패해도 `report-specialist`는 반드시 호출한다. RCA가
미확정이면 Remediation을 호출하지 않고 `NOT_ATTEMPTED` 결과를 Report에 전달한다.

## 실행 격리

각 호출은 빈 실행별 산출물 디렉터리에서 시작하는 독립 RCA이다. 이전 호출의
산출물을 탐색하거나 읽지 않는다. 이전 호출의 산출물을 재사용하지 않는다. 세 전문 에이전트는 현재 실행
토큰으로만 제한된 MCP 도구를 사용한다.

## 역할과 도구

| 역할 | 허용 도구 | 책임 |
|------|----------|------|
| 메인 오케스트레이터 | Agent, Skill | 순서와 결과 전달 |
| RCA | 읽기 전용 AWS/GitHub MCP, `save_artifact` | 스코핑, 가설, 검증 |
| Remediation | `execute_healthcare_reset` | 확정 원인과 일치하는 Healthcare reset 요청 |
| Report | `save_artifact` | RCA+복구 결과로 `report.md`, `playbook.json` 저장 |

메인 세션은 `orchestrator` agent로 실행되며 `Agent(rca-specialist,
remediation-specialist, report-specialist)`와 `Skill`만 허용한다. reset MCP 도구는
`remediation-specialist`의 도구 목록에만 존재한다.

RCA와 Report는 HTTP, Bash, ECS 변경을 실행하지 않는다. Remediation도 URL, 셸 명령,
ECS 액션을 받거나 만들지 않고 narrow MCP 도구에 허용된 fault type만 전달한다.
복구 도구는 최신 validation 산출물의 `confirmed`를 서버에서 다시 검증한다.

## 산출물

| 파일명 | 작성 주체 |
|--------|----------|
| `scoping.json` | RCA |
| `hypotheses.json` | RCA |
| `validation-{N}.json` | RCA |
| `remediation.json` | narrow MCP 도구 |
| `report.md` | Report |
| `playbook.json` | Report |

모든 JSON은 valid object여야 한다. 경로 직접 접근과 임의 파일 생성·수정·삭제는
금지하며, 산출물 저장은 `save_artifact`만 사용한다.

## 금지 사항

- 셸 명령 금지
- 임의 HTTP 요청 금지
- ECS `UpdateService`, force deployment, 재시작, 롤백 실행 금지
- 미확정 또는 unsupported 원인의 대체 복구 금지
- 복구 실패를 이유로 Report 생략 금지
- 수행하지 않은 복구나 정상화를 보고서에 성공으로 기록 금지
