# Decision Log: agent

이 문서는 agent 카테고리의 주요 결정 변경 이력이다. 각 ADR 본문은 현재 상태만
서술하고, 개별 diff는 Git이 보존한다.

| 날짜 | 변경 | 근거 | 현재 ADR |
|------|------|------|----------|
| 2026-07-21 | 복구 결과와 보고서 계약을 모델 출력 중심에서 서버 소유 검증 결과 중심으로 강화 | 실제 CloudWatch 정상화와 최종 산출물의 상태 불일치를 완료 전에 차단하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | CC Headless를 단일 프롬프트 실행에서 RCA·Remediation·Report 전문 서브 에이전트 오케스트레이션으로 전환 | 분석·쓰기 권한과 단계별 실패 경계를 분리하면서 한 실행의 컨텍스트를 유지하기 위해 | [CC Headless 오케스트레이션](0011-cc-headless-prompt-driven-rca.md) |
| 2026-07-21 | CC Headless 복구를 외부 공통 워커에서 실행 내부의 제한된 Remediation 서브 에이전트로 전환 | 별도 메시지 홉을 제거하되 확정 산출물과 허용 목록을 도구에서 강제하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
