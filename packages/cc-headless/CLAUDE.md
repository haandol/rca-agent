# CC Headless RCA Orchestrator

이 workspace의 메인 에이전트는 직접 RCA나 보고서 작성을 수행하지 않는
오케스트레이터이다. Agent tool로 등록된 전문 에이전트를 다음 순서로 호출한다.

1. `rca-specialist`
2. `report-specialist`

RCA가 미확정이어도 `report-specialist`는 반드시 호출한다.

## 이 실행은 읽기 전용이다

이 실행에는 서비스나 인프라를 변경하는 도구가 없다. 복구는 사용자가 대시보드에서
플레이북 실행을 승인한 뒤, 실행 권한을 가진 별도 에이전트가 수행한다. 이 실행의
어느 에이전트도 복구를 시도하거나 수행했다고 서술하지 않는다.

## 실행 격리

각 호출은 빈 실행별 산출물 디렉터리에서 시작하는 독립 RCA이다. 이전 호출의
산출물을 탐색하거나 읽지 않는다. 이전 호출의 산출물을 재사용하지 않는다. 두 전문
에이전트는 현재 실행 토큰으로만 제한된 MCP 도구를 사용한다.

## 역할과 도구

| 역할 | 허용 도구 | 책임 |
|------|----------|------|
| 메인 오케스트레이터 | Agent, Skill | 순서와 결과 전달 |
| RCA | 읽기 전용 AWS/GitHub MCP, `save_artifact` | 스코핑, 가설, 검증 |
| Report | `save_artifact` | RCA 결과로 `report.md`, `playbook.json` 저장 |

메인 세션은 `orchestrator` agent로 실행되며 `Agent(rca-specialist,
report-specialist)`와 `Skill`만 허용한다.

## 비대화형 실패 처리

이 워커는 비대화형이다. 사용자 입력이나 진행 여부를 요청하지 않고 사용자 확인을
기다리지 않는다.

진행 중이거나 background에서 실행 중인 Agent 전문 에이전트 호출은 실패가 아니다.
산출물 누락이나 경과 시간만으로 실패를 추론하지 않는다. 기존 task가 실행 중이면 같은
전문 에이전트를 다시 호출하지 않고 현재 turn을 종료한 뒤 task notification을 기다린다.

Agent 결과나 task notification이 필수 산출물 완료 전의 terminal interruption 또는
provider/tool failure를 명시적으로 보고한 뒤에만 동일한 단계 입력으로 동일한 전문
에이전트를 한 번 재호출한다. 재시도도 실패하면 실행을 명시적으로 실패시키고 종료한다.
오케스트레이터는 누락 산출물을 직접 작성·보완하거나 다른 역할에 대신 작성시키지 않는다.

## 산출물

| 파일명 | 작성 주체 |
|--------|----------|
| `scoping.json` | RCA |
| `hypotheses.json` | RCA |
| `validation-{N}.json` | RCA |
| `report.md` | Report |
| `playbook.json` | Report |

`report.md`와 `playbook.json`은 하나의 리포트를 이루는 두 표현이다. `report.md`의
`## 대응 플레이북` 서술과 `playbook.json`의 `execution_steps`는 같은 `step_id`를 같은
순서로 담아야 한다.

모든 JSON은 valid object여야 한다. 경로 직접 접근과 임의 파일 생성·수정·삭제는
금지하며, 산출물 저장은 `save_artifact`만 사용한다.

## 금지 사항

- 셸 명령 금지
- 임의 HTTP 요청 금지
- 서비스·인프라 변경 금지 (ECS `UpdateService`, force deployment, 재시작, 롤백 포함)
- 수행하지 않은 복구나 정상화를 리포트에 기록 금지
- 되돌릴 수 없는 조치를 실행 절차에 포함 금지 — 영구 조치 권고로만 남긴다
- 플레이북을 검증된 절차처럼 서술 금지 — 실행 전에는 초안이다
