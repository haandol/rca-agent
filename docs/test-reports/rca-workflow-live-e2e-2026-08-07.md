# RCA Workflow Live E2E - 2026-08-07

## 판정

전체 판정은 **부분 통과**다. 최초 증상 알람의 dual-engine 분석, 산출물, 승인 게이트,
중복 승인 방지, 실행 증거 보존, fail-closed 판정, 미해결 실행 비승격, cleanup 계약은
통과했다. 실행은 `UNRESOLVED`이므로 RESOLVED -> 회고 -> 플레이북/인덱스 승격 경로는
성립하지 않았다.

## 실행 식별자와 배포 상태

- `RUN_ID`: `deployed-e2e-20260807T133048Z`
- `TEST_STARTED_AT`: `2026-08-07T13:30:48+00:00`
- Git HEAD / agent, cc-headless, execution 이미지 태그: `ee4f0ce` /
  `ee4f0ceb9d4f`
- 시작 시 원본 DB parameter group: `default.postgres17`
- 시작 시 세 워커: 모두 ACTIVE, desired/running `1/1`, pending `0`, rollout
  `COMPLETED`
- red-herring: task definition `RcaAgentDevHealthcare:50`,
  `13:31:26Z`~`13:34:32Z`
- db-leak: task definition `RcaAgentDevHealthcare:51`,
  `13:37:55Z`~`13:41:00Z`

## 알람과 분석

- `RcaAgentDev-Healthcare-RdsHighConnections`: `13:41:32Z` ALARM
- `RcaAgentDev-Healthcare-VitalIngestFailures`: `13:42:04.471Z` ALARM
- 최초 증상 알람 RCA partition:
  `RCA#e2574676-16b7-575e-8b13-df9d543fdee1`
- `strands#SESSION`: created `13:42:04.590Z`, `COMPLETED`,
  confirmed root cause, DRAFT playbook `de849af1-650d-4180-b093-0de507a5d633`,
  execution steps 4
- `cc-headless#SESSION`: created `13:42:04.588Z`, `COMPLETED`
  (`14:06:16Z`), confirmed root cause, DRAFT playbook
  `RcaAgentDev-Healthcare-VitalIngestFailures-20260807T134204Z`,
  execution steps 3
- 두 세션의 idempotency key와 `alarm_data.StateChangeTime`은 모두 최초 증상
  알람 시각 `13:42:04.471Z`를 가리켰다.
- Strands report:
  `reports/strands/e2574676-16b7-575e-8b13-df9d543fdee1/attempt-1-922864866b9d4b51ade38e37fa7db4c6/report.md`
- CC report:
  `reports/cc-headless/e2574676-16b7-575e-8b13-df9d543fdee1/attempt-1-9d2a7e33a06042fc8d52fbbc7a096ae5/report.md`
- `RdsHighConnections`로 생성된 신규 세션: 0

실행 중 Healthcare 재배포가 일시적으로 실패하면서 증상 알람이 `14:00:04.475Z`에
재발화했다. 이 별도 전환은 `RCA#2745f272-f579-5ab8-9e10-d6a4c1185ff5`에 추가
Strands/CC 세션을 만들었다. `14:11:46Z` 관측 시 각각 `EVIDENCE_COLLECTION`과
`ANALYZING`이었다. 최초 알람 lineage는 정확했지만, 전체 실행 동안 신규 세션이 정확히
한 쌍뿐이라는 조건은 충족하지 않았다.

## 승인과 실행

- 승인 엔진: Strands
- approval/execution ID:
  `db4d1a52-9824-4a75-952b-9c52b49fa0e8`
- 최초 승인: HTTP 200, 실행 절차 4개, 승인 스냅샷 저장
- 동일 approval ID 재전달: HTTP 409
- 생성된 `EXEC#` 항목: 1개
- 실행 시간: `13:54:51Z`~`14:04:35Z`
- 최종 서버 판정: `UNRESOLVED`
- durable evidence:
  `executions/e2574676-16b7-575e-8b13-df9d543fdee1/db4d1a52-9824-4a75-952b-9c52b49fa0e8/evidence.json`
- 요약: attempted steps 3, blocked 1, failed 0,
  `resolution_recorded=true`
- 차단: `terminate_idle_sessions`의 ECS Exec 시도가 self-control /
  privilege-escalation으로 거부되어 manual action으로 남았다.
- 미시도: `verify_ingest_recovery`의 attempts가 0이어서
  `steps were not attempted: verify_ingest_recovery`로 fail-closed 처리됐다.
- 에이전트 서술은 해결을 주장했지만 서버가 `UNRESOLVED`로 확정했다.
- `EXEC_ACTIVE`: 없음
- `strands#PLAYBOOK_REVISION`: 없음
- `playbook_indexed` / `retrospective_updated_playbook`: 없음

따라서 증거 보존과 false-resolution 방지 계약은 통과했고, 미해결 실행이 회고 또는
검색 인덱스로 승격되지 않는 계약도 통과했다. 해결 실행의 회고/승격 계약은 이번 결과로
검증되지 않았다.

## Cleanup

- 명령: 동일 `RUN_ID`와 `--restore-db-parameter-group default.postgres17`로
  `inject_deployment_fault.py cleanup` 실행
- 실행 시간: `14:06:29Z`~`14:10:07Z`
- 결과: `clean=true`, service stable, task definition
  `RcaAgentDevHealthcare:52`
- 최종 fault flags: `FAULT_DB_LEAK=false`, `FAULT_ERROR_RATE=0.0`,
  `FAULT_SLOW_QUERY_MS=0`
- DB: `available`, `default.postgres17`, parameter apply `in-sync`
- 이번 실행 전후 커스텀 DB parameter group: 0개; 삭제한 그룹 없음
- Healthcare 및 agent/cc-headless/execution 서비스: 모두 ACTIVE,
  desired/running `1/1`, pending `0`, rollout `COMPLETED`
- 최종 알람: `RdsHighConnections=OK`, `VitalIngestFailures=OK`
- 로컬 dashboard 종료 확인: port 3100 미수신

Cleanup 계약은 통과했다.
