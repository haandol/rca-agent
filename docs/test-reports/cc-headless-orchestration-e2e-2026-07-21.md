# CC Headless Orchestration E2E Report

## 판정

- 최종 판정: **PASS WITH FOLLOW-UPS**
- 대상 리전: `us-east-1`
- AWS 계정: `395271362395`
- 성공 세션: `bb4328a4-6ca1-5df2-91e1-0a7b8f4bdd7b`
- 검증 흐름: `RCA specialist -> conditional Remediation specialist -> Report specialist`
- 장애 유형: RDS DB connection leak

핵심 기능은 실제 AWS 환경에서 끝까지 성공했다. 알람 발생, RCA 확정, 허용된 reset,
보고서와 플레이북 생성, S3 저장, 세션 완료, 알람 정상화가 모두 확인됐다. 다만 세션
reclaim 시 외부 부작용 fencing과 사후 관측 결과의 보고서 반영은 운영 전 보완이 필요하다.

## 배포

| 항목 | 결과 |
|---|---|
| CC Headless ECR | `395271362395.dkr.ecr.us-east-1.amazonaws.com/rcaagentdev/cc-headless:latest` |
| 최종 image digest | `sha256:c61caff10e01981b8da1c4dae34c101a6881feeb22c30efc71cf9ccfb2e1d796` |
| 서비스 | `RcaAgentDevCcHeadless` |
| 리전 | `us-east-1` |
| desired/running | `1/1` |
| Strands Remediation 서비스 | desired/running `0/0` |
| 서울 리전 | `RcaAgentDev*` 스택 없음 |

## 첫 실행과 RCA

### 실패 증상

첫 장애 주입은 RDS `DatabaseConnections`를 `2 -> 37`로 올렸고 알람도 정상 발화했다.
CC CLI는 483초 뒤 `rc=0`으로 종료했지만 watcher의 `seen_count=0`이었고
`report.md`가 없어 세션이 `FAILED`가 됐다. 자동 reset도 실행되지 않아 테스트 종료
절차로 `/fault/db-leak/reset`을 수동 호출해 35개 연결을 정리했다.

### 근본 원인

`mcp-config.json`의 `rca-progress` 서버가 `python -m fastmcp`로 실행되고 있었다.
배포된 FastMCP 패키지는 `fastmcp.__main__`을 제공하지 않아 서버가 시작 직후
종료됐다. 따라서 전문 에이전트는 `save_artifact`와
`execute_healthcare_reset`을 사용할 수 없었다.

AWS Knowledge 서버도 `UV_OFFLINE=true` 환경에서 `uvx fastmcp` 의존성 해석을
시도해 시작에 실패했다.

### 조치

- `rca-progress`: `fastmcp run /app/src/cc_headless/mcp_server.py:mcp`
- `aws-knowledge`: `fastmcp run https://knowledge-mcp.global.api.aws`
- MCP 실행 방식 계약 테스트 추가
- 필수 보고서 누락 시 Claude 최종 응답 일부를 진단 로그에 포함
- 새 태스크에서 `report-specialist`의 실제 `save_artifact` 호출 스모크 테스트 수행

## 성공 E2E 타임라인

| UTC | 이벤트 | 증거 |
|---|---|---|
| 08:19 | DB leak 35개 주입 | RDS connections `2 -> 37` |
| 08:21:37 | CloudWatch `ALARM` | 연속 2개 datapoint `37` |
| 08:21:37 | CC 세션 시작 | 상태 `ANALYZING` |
| 08:23:40 | Scoping 저장 | `cc-headless#SPAN`, COMPLETED |
| 08:24:22 | Hypotheses 저장 | 가설 4개 |
| 08:25:13 | Validation 저장 | H1 확정, confidence `0.95` |
| 08:26:03 | 자동 reset | `POST /fault/db-leak/reset`, HTTP 200 |
| 08:26:04 | Remediation 저장 | `SUCCEEDED` |
| 08:26 | RDS 정상화 | connections `37 -> 2` |
| 08:27:37 | CloudWatch `OK` | datapoint `2` |
| 08:28:01 | Report 저장 | `report.md` |
| 08:28:31 | Playbook 저장 | `playbook.json` |
| 08:29:32 | 세션 완료 | 상태 `COMPLETED`, 총 474초 |

## 산출물 검증

- DynamoDB에 `SCOPING`, `HYPOTHESIS_GENERATION`, `VALIDATION_LOOP`,
  `REMEDIATION`, `REPORT`, `PLAYBOOK` 스팬 6개가 모두 `COMPLETED`로 저장됐다.
- Remediation 결과는 `SUCCEEDED`, endpoint는 `/fault/db-leak/reset`이다.
- S3 보고서:
  `s3://rca-agent-dev-evidence/reports/cc-headless/bb4328a4-6ca1-5df2-91e1-0a7b8f4bdd7b.md`
- S3 객체 크기 `7,073 bytes`, content type `text/markdown`.
- 보고서는 확정 원인 confidence `0.95`와 실제 reset 결과를 포함한다.
- 플레이북 ID: `a4f2e8b1-7d3c-4e9a-b5f6-2c8d1e0f4a7b`.

## 테스트

| 범위 | 결과 |
|---|---|
| CC Headless | `139 passed` |
| CC Headless Ruff | pass |
| Healthcare | `13 passed, 1 xfailed` |
| Infra | `14 passed` |
| Strands Agent | `365 passed, 13 xfailed` |
| Root contracts | evaluation fixture digest drift 1건 실패 |

Root contract 실패는 현재 fixture digest
`sha256:b45196...`와 승인 baseline `sha256:12e043...`의 차이다. 이번 작업에서
baseline은 승인하지 않았다.

## 추가로 수정한 결함

완료 세션의 `root_cause`가 실제 원인이 아니라 보고서의 첫 메타데이터 줄
`상태: 확정`으로 저장되는 문제가 E2E에서 발견됐다. 근본 원인 섹션에서 상태,
신뢰도, 가설 메타데이터를 건너뛰고 실제 설명을 추출하도록 수정했으며 회귀 테스트를
추가했다. 이 수정은 최종 digest에 포함됐다.

## 남은 위험과 우선순위

### P0 - 세션 reclaim fencing

- 이전 claim 소유자가 reset, S3 upload, SNS publish, watcher write를 수행할 수 있는
  짧은 경합 구간이 남아 있다.
- DynamoDB 소유권 조회 실패가 fail-open으로 처리된다.
- 조치: 외부 부작용 직전 claim 검증, attempt별 S3 key, 조건부 publish/outbox,
  watcher claim 조건을 도입한다.

### P1 - 복구 후 검증

- 실제 메트릭과 알람은 정상화됐지만 생성된 보고서는 `관측 대기`로 남았다.
- 조치: reset 이후 제한된 CloudWatch 검증 단계를 추가하고
  `NORMALIZED / FAILED / PENDING`을 서버 소유 결과로 저장한다.

### P1 - 산출물 정합성

- pipeline 완료 게이트는 보고서/플레이북의 의미 스키마와 서버 소유
  `remediation.json`의 일치 여부를 충분히 검증하지 않는다.
- 조치: JSON schema와 report 필수 섹션을 검증하고 remediation status,
  fault type, endpoint, validation artifact를 교차 확인한다.

### P1 - 비활성 Strands remediation

- 서비스 desired count는 0이지만 코드에는 이벤트 `confirmed` 신뢰, ECS force
  deployment fallback, 비멱등 처리, 예외 후 ACK 동작이 남아 있다.
- 조치: 스택을 완전히 제거하거나, 다시 활성화할 가능성이 있으면 CC와 동일한
  fail-closed 계약으로 정리한다.

### P2 - 보고서 시간 범위

- 성공 보고서가 같은 날 앞선 수동 테스트의 로그도 RCA 증거로 포함했다.
- 조치: 현재 alarm window와 과거 비교 window를 보고서에서 명시적으로 분리한다.

### P2 - 기타

- Dashboard에 remediation 상태를 노출한다.
- evaluation fixture digest drift를 검토 후 명시적으로 승인하거나 fixture를 수정한다.
- 구형 단일 프롬프트 설명이 남은 상세 아키텍처 문서를 동기화한다.

## 관련 리뷰

항목별 ADR 구현 검토:
`docs/test-reports/adr-impl-review-2026-07-21.html`
