# Decision Log: agent

이 문서는 agent 카테고리의 주요 결정 변경 이력이다. 각 ADR 본문은 현재 상태만
서술하고, 개별 diff는 Git이 보존한다.

| 날짜 | 변경 | 근거 | 현재 ADR |
|------|------|------|----------|
| 2026-07-25 | CC 하네스 자산을 실험용·배포용 이중 관리에서 엔진 패키지 단일 소스로 고정 | 로컬에서 검증한 오케스트레이션 동작이 배포 동작을 그대로 보증하도록 하기 위해 | [CC Headless 오케스트레이션](0011-cc-headless-prompt-driven-rca.md), [CC Headless 실행 인프라](../infra/0003-lambda-cc-headless-stack.md) |
| 2026-07-21 | CC 자동 복구 전에 다른 허용 액션의 경쟁 가설이 기각·종료됐는지 검증 | 확정 가설 하나만으로 조사 중인 상충 원인을 무시하고 잘못된 reset을 실행하지 않기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | CC 복구 후 정상화 판정을 원본 CloudWatch 알람의 M-of-N 및 missing-data 정책에 맞춤 | 단일 정상 datapoint만으로 아직 ALARM 조건인 장애를 정상화로 보고하지 않기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | CC 자동 복구를 서버 소유 알람의 Healthcare 리소스 식별자와 fault별 metric에 바인딩 | 다른 서비스 알람이나 같은 metric의 다른 리소스가 Healthcare reset을 유발하지 못하도록 하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | Strands 복구 결과 발행을 저장 우선 durable outbox와 publication lease로 전환 | 완료 저장보다 SNS 발행이 앞서 재전달 시 reset과 결과 이벤트가 반복되는 경합을 차단하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md), [세션 복구](../infra/0006-session-recovery-on-restart.md) |
| 2026-07-21 | Strands 자동 복구 유형을 초기 가설 분류에서 증거 기반 검증 유형으로 전환 | 초기 모델 오분류가 확정 가설의 설명·증거와 다른 리셋 액션을 선택하지 못하도록 하기 위해 | [가설 트리 라이프사이클](0002-hypothesis-tree-lifecycle.md), [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | 복구 결과와 보고서 계약을 모델 출력 중심에서 서버 소유 검증 결과 중심으로 강화 | 실제 CloudWatch 정상화와 최종 산출물의 상태 불일치를 완료 전에 차단하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | CC Headless를 단일 프롬프트 실행에서 RCA·Remediation·Report 전문 서브 에이전트 오케스트레이션으로 전환 | 분석·쓰기 권한과 단계별 실패 경계를 분리하면서 한 실행의 컨텍스트를 유지하기 위해 | [CC Headless 오케스트레이션](0011-cc-headless-prompt-driven-rca.md) |
| 2026-07-21 | CC Headless 복구를 외부 공통 워커에서 실행 내부의 제한된 Remediation 서브 에이전트로 전환 | 별도 메시지 홉을 제거하되 확정 산출물과 허용 목록을 도구에서 강제하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
