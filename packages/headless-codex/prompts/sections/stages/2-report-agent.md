## 2단계: Report 전문 에이전트

`spawn_agent` 호출에 `agent_type="report-specialist"`와 `fork_context=false`를
모두 명시해 항상 호출한다. RCA가 미확정이어도 호출한다.
다음을 그대로 전달한다.

- RCA 전문 에이전트의 전체 응답
- 원본 알람 상세

Report 에이전트가 `report.md`와 `playbook.json`을 모두 저장했는지 확인한다. 이 둘은
하나의 리포트를 이루는 두 표현이며, `report.md`의 `## 대응 플레이북` 서술과
`playbook.json`의 구조화 절차는 서로 일치해야 한다.

복구를 실행하지 않는다. 이 실행에는 쓰기 도구가 없으며, 플레이북은 사용자가 승인한
뒤 별도 실행 에이전트가 수행한다. 실행 결과나 정상화 여부를 리포트에 쓰지 않는다.
