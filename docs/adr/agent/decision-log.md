# Decision Log: agent

이 문서는 agent 카테고리의 주요 결정 변경 이력이다. 각 ADR 본문은 현재 상태만
서술하고, 개별 diff는 Git이 보존한다.

| 날짜 | 변경 | 근거 | 현재 ADR |
|------|------|------|----------|
| 2026-07-30 | 기본 모델을 Sonnet 4.6에서 Sonnet 5 세대로 올리고, sampling 파라미터와 effort 수준 전달을 금지 | 새 세대가 `temperature`를 거부하고 대상 Bedrock 배포가 `effort`를 허용하지 않아, 기존 호출 표면을 유지하면 모든 LLM 호출이 검증 오류로 실패하므로 | [모델 티어 아키텍처](0010-model-tier-architecture.md) |
| 2026-07-30 | 자동 복구의 실행 근거를 플레이북 절차에서 검증 확정 원인 유형과 서버 허용 목록으로 확정 | LLM이 생성한 자유 텍스트 절차가 쓰기 액션의 권위가 되면 fail-closed 경계가 무너지므로 | [플레이북 생성](0008-playbook-generation.md), [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-04-22 | 모델 구성을 경량·고성능 2-tier에서 단일 모델 + thinking 유무 행동 분리로 전환 | 경량 모델이 도구 응답과 구조화 출력에서 토큰 한도에 걸려 증거를 누락하고, 확정 금지 가드레일 재트리거가 절감을 상쇄했기 때문에 | [모델 티어 아키텍처](0010-model-tier-architecture.md) |
| 2026-04-28 | 초기 스코핑의 유사도 검색 대상을 플레이북 인덱스에서 RCA 보고서 인덱스로 교체 | 플레이북은 대응 절차 중심이라 가설 생성에 필요한 "증상 → 근본 원인" 추론 경로가 빈약했으므로 | [초기 스코핑과 보고서 유사도](0001-initial-scoping-and-report-similarity.md), [플레이북 생성](0008-playbook-generation.md) |
| 2026-07-25 | CC 하네스 자산을 실험용·배포용 이중 관리에서 엔진 패키지 단일 소스로 고정 | 로컬에서 검증한 오케스트레이션 동작이 배포 동작을 그대로 보증하도록 하기 위해 | [CC Headless 오케스트레이션](0011-cc-headless-prompt-driven-rca.md), [CC Headless 실행 인프라](../infra/0003-lambda-cc-headless-stack.md) |
| 2026-07-21 | CC 자동 복구 전에 다른 허용 액션의 경쟁 가설이 기각·종료됐는지 검증 | 확정 가설 하나만으로 조사 중인 상충 원인을 무시하고 잘못된 reset을 실행하지 않기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | CC 복구 후 정상화 판정을 원본 CloudWatch 알람의 M-of-N 및 missing-data 정책에 맞춤 | 단일 정상 datapoint만으로 아직 ALARM 조건인 장애를 정상화로 보고하지 않기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | CC 자동 복구를 서버 소유 알람의 Healthcare 리소스 식별자와 fault별 metric에 바인딩 | 다른 서비스 알람이나 같은 metric의 다른 리소스가 Healthcare reset을 유발하지 못하도록 하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | Strands 복구 결과 발행을 저장 우선 durable outbox와 publication lease로 전환 | 완료 저장보다 SNS 발행이 앞서 재전달 시 reset과 결과 이벤트가 반복되는 경합을 차단하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md), [세션 복구](../infra/0006-session-recovery-on-restart.md) |
| 2026-07-21 | Strands 자동 복구 유형을 초기 가설 분류에서 증거 기반 검증 유형으로 전환 | 초기 모델 오분류가 확정 가설의 설명·증거와 다른 리셋 액션을 선택하지 못하도록 하기 위해 | [가설 트리 라이프사이클](0002-hypothesis-tree-lifecycle.md), [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | 복구 결과와 보고서 계약을 모델 출력 중심에서 서버 소유 검증 결과 중심으로 강화 | 실제 CloudWatch 정상화와 최종 산출물의 상태 불일치를 완료 전에 차단하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
| 2026-07-21 | CC Headless를 단일 프롬프트 실행에서 RCA·Remediation·Report 전문 서브 에이전트 오케스트레이션으로 전환 | 분석·쓰기 권한과 단계별 실패 경계를 분리하면서 한 실행의 컨텍스트를 유지하기 위해 | [CC Headless 오케스트레이션](0011-cc-headless-prompt-driven-rca.md) |
| 2026-07-21 | CC Headless 복구를 외부 공통 워커에서 실행 내부의 제한된 Remediation 서브 에이전트로 전환 | 별도 메시지 홉을 제거하되 확정 산출물과 허용 목록을 도구에서 강제하기 위해 | [자동 복구 실행 경계](0012-automated-remediation.md) |
