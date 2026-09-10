## 2단계: Report 전문 에이전트

`spawn_agent` 호출에 `agent_type="report-specialist"`와 `fork_context=false`를
모두 명시해 항상 호출한다. RCA가 미확정이어도 호출한다.
다음을 그대로 전달한다.

- RCA 전문 에이전트의 전체 응답
- 원본 알람 상세

{{include: ../roles/report.md}}
