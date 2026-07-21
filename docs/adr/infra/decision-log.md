# Decision Log: infra

이 문서는 infra 카테고리의 주요 결정 변경 이력이다. 각 ADR 본문은 현재 상태만
서술하고, 개별 diff는 Git이 보존한다.

| 날짜 | 변경 | 근거 | 현재 ADR |
|------|------|------|----------|
| 2026-07-21 | 세션 claim fencing을 상태 전이에서 외부 부작용 시작 경계까지 확장 | reclaim된 이전 실행의 reset·보고서·알림·trace 쓰기를 차단하기 위해 | [세션 복구](0006-session-recovery-on-restart.md) |
| 2026-07-21 | 미완료 CC 세션 복구를 단순 중복 확인에서 SQS receive count와 claim token 기반 원자적 reclaim으로 강화 | 재전달을 실제 재실행으로 연결하면서 이전 실행의 늦은 상태·결과 확정을 차단하기 위해 | [세션 복구](0006-session-recovery-on-restart.md) |
| 2026-07-21 | CC Headless 전용 외부 복구 홉 대신 동일 Fargate 실행 안의 제한된 복구 도구를 채택 | 분석 컨텍스트를 유지하고 복구 지연을 줄이면서 일반 인프라 쓰기 권한을 제거하기 위해 | [CC Headless 실행 인프라](0003-lambda-cc-headless-stack.md) |
