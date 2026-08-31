# 공용 알람 큐 단일 소유권 Live E2E — 2026-08-31

## 판정

- **공용 큐·단일 분석 소유권: PASS**
- **분석부터 실행·회고까지 닫힌 루프: 미통과**
- **Cleanup: PASS**

운영 알람 하나가 `ANALYSIS#SESSION` 한 건만 생성하고 Strands 한 엔진만 분석했다.
분석은 근본 원인을 미확정으로 종료해 실행 절차를 만들지 않았으며, 승인·실행·회고는
안전 계약에 따라 시작되지 않았다.

## 범위

- 배포 커밋: `0ab2a495a90f`
- 시나리오: `deployed-connection-leak-vital-ingest`
- 정상 검증 RUN_ID: `deployed-e2e-single-owner-20260831T055719Z`
- 증거 디렉터리:
  `/tmp/deployed-e2e-single-owner-20260831T055719Z.3Z6G3j`
- 검증 시작: `2026-08-31T05:57:28.864885Z`
- 검증 종료: `2026-08-31T06:40:46.442702Z`

첫 시도 `deployed-e2e-single-owner-20260831T053949Z`는 validation shell의 AWS CLI
pager를 중단하면서 SIGINT로 종료됐다. 안전 드라이버가 cleanup을 완료했고
`clean=true`를 확인한 후 새 RUN_ID로 다시 실행했다.

## 배포 게이트

| 서비스 | 태스크 정의 | 이미지 | 최종 상태 |
|---|---:|---|---|
| Strands | `RcaAgentDevRcaAgent:23` | `rca-agent:0ab2a495a90f` | `ACTIVE`, `1/1`, rollout `COMPLETED` |
| Headless Codex | `RcaAgentDevCcHeadless:34` | `cc-headless:0ab2a495a90f` | `ACTIVE`, `1/1`, rollout `COMPLETED` |
| Playbook Execution | `RcaAgentDevPlaybookExecution:22` | `cc-headless:0ab2a495a90f` | `ACTIVE`, `1/1`, rollout `COMPLETED` |

- 알람 SNS의 SQS 구독: `RcaAgentDevAlarmQueue` 한 건
- 공용 큐 visibility timeout: 3900초
- 기존 `RcaAgentDevCcHeadlessQueue`: 삭제 확인

## 장애·알람 계보

| 단계 | 태스크 정의 | 완료 시각 |
|---|---|---|
| Red herring (`LOG_LEVEL=DEBUG`) | `RcaAgentDevHealthcare:71` | `2026-08-31T06:01:09.686335Z` |
| DB leak (`FAULT_DB_LEAK=true`) | `RcaAgentDevHealthcare:72` | `2026-08-31T06:06:52.760716Z` |
| Cleanup | `RcaAgentDevHealthcare:73` | `2026-08-31T06:39:34.674913Z` |

- 증상 알람 `OK → ALARM`: `2026-08-31T06:08:22.135Z`
- 원인 알람 `OK → ALARM`: `2026-08-31T06:08:29.331Z`
- `VitalIngestFailures`: 06:06 5건, 06:07 7건 이후 지속 발생
- `DatabaseConnections`: 15 이상으로 상승, 최대 27 관측
- `DB session not returned to the pool` 로그: 105건
- CloudTrail에서 red-herring과 fault 각각의
  `RegisterTaskDefinition`·`UpdateService`를 확인
- 배포 코드 `database_adapter.py`의 `leaky_session()`이 세션을 닫지 않는 경로 확인

## 단일 세션 소유권

| 항목 | 값 |
|---|---|
| RCA ID | `21f14ce6-68cf-5450-85a0-04e91eb03263` |
| PK | `RCA#21f14ce6-68cf-5450-85a0-04e91eb03263` |
| SK | `ANALYSIS#SESSION` |
| Engine | `strands` |
| Receive count | `1` |
| State | `COMPLETED` |
| Created | `2026-08-31T06:08:22.393550Z` |
| Completed | `2026-08-31T06:32:58.288369Z` |

`fault.completedAt` 이후 모든 세션 키를 조회한 결과는 이 한 건뿐이었다. 엔진별
`strands#SESSION`·`headless-codex#SESSION` 레코드는 생성되지 않았고,
`RcaAgentDev-Healthcare-RdsHighConnections` 원인 알람도 세션을 만들지 않았다.

## 분석 결과

- 근본 원인: 커넥션 미반환에 의한 DB connection leak 최우선 후보
- 확정 여부: `false`
- 신뢰도: 약 0.55
- 저장 fault type: `UNSUPPORTED`
- 플레이북 상태: `DRAFT`
- 실행 절차: 0건
- 실행 항목: 0건

메트릭·로그는 커넥션 누수를 강하게 지지했지만, 여러 증거 수집이 timeout으로 끝나
현재 장애를 유발한 배포와 코드 변경을 분석 안에서 직접 검증하지 못했다. 리포트는 이를
명시하고 미확정으로 종결했다. 따라서 승인 API를 호출하지 않았고 duplicate approval,
실행 증거, 해결 판정, 회고·검색 인덱스 승격은 이번 회차에서 검증하지 않았다.

## Cleanup

Manifest의 validation exit code와 전체 exit code는 모두 0이며 cleanup 결과는
`clean=true`다.

- 모든 fault flag 해제
- `RCA_TEST_RUN_ID`·`RCA_TEST_PHASE` 제거
- Healthcare 서비스 안정화 `1/1`
- DB 상태 `available`
- DB parameter group `default.postgres17`, `in-sync`
- 두 Healthcare 알람 `OK`
- 활성 RUN_ID 없음
- 삭제 대상 임시 DB parameter group 없음

## 발견사항

### Medium — 시나리오의 분석·실행 닫힌 루프 미완주

시나리오는 confirmed root cause와 실행 가능한 remediation을 요구하지만, Strands의
증거 수집 timeout으로 미확정 리포트가 생성됐다. 안전 게이트는 정상적으로 동작했으나
승인 이후 경로는 검증되지 않았다.

완료 조건:

1. 배포·CloudTrail·배포 코드 증거가 시간 예산 안에 수집된다.
2. root cause가 `db-leak`으로 확정된다.
3. rollback 중심 실행 절차가 생성된다.
4. 고정 approval ID의 두 번째 요청이 409가 된다.
5. 실행 증거를 근거로 RESOLVED/UNRESOLVED가 판정되고, RESOLVED일 때만 회고와
   검색 인덱스 승격이 확인된다.

### Low — cleanup 후 공용 큐에 in-flight 메시지 한 건

가시 메시지는 0건이지만 not-visible 메시지 한 건이 남았다. 첫 중단 회차의 취소된
세션 메시지일 가능성이 높으며, visibility 만료 후 terminal duplicate 또는 이전
이벤트 억제로 확인 처리될 것으로 예상된다. 새 분석 세션이나 중복 실행은 관측되지 않았다.

## 다음 작업

Strands 증거 수집의 CloudTrail·배포 코드 조회 timeout을 진단하고, 같은 시나리오를
재실행해 승인·실행·회고까지 닫힌 루프를 완주한다.
