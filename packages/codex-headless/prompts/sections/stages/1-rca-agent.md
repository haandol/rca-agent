## 1단계: RCA 전문 에이전트

`spawn_agent` 호출에 `agent_type="rca-specialist"`와 `fork_context=false`를 모두
명시한다. 알람 상세를 모두 전달하고, 읽기 전용
증거 수집부터 최종 validation까지 수행하도록 요청한다.

현재 알람 상태 변경 시각을 기준으로 current alarm window를 먼저 고정하고, baseline
조회는 동일 길이의 historical comparison window로 별도 표시한다. 모든 증거에는
window와 관측 시각을 붙인다. current alarm window 이전 수동 테스트 로그는 현재
장애의 증거로 사용하지 않는다.

응답에서 마지막 validation의 `confirmed`를 확인한다. 단순 자연어 주장이나 보고서
문구를 확정 근거로 사용하지 않는다.
