# RCA Evaluation Harness

루트 하네스는 오프라인 계약 테스트, fixture 구조 회귀, 실모델 계약 평가를
분리한다. 실제 배포 이벤트 전달과 증거 탐색은 별도의 배포 E2E로 검증한다.

## 현실적 시나리오 카탈로그 — 로컬 실측 반영, 알람 보정 대기

현재 `tests/scenarios/`는 HTTP 오류 로그 보호까지 반영한 서비스의 실제 로컬
PostgreSQL 재현 결과(`proof-review-fixed-01`)와 측정 소스 해시를 사용한다.
**알람 외곽 입력은 합성 예시이며 AWS에서 수집한 알람이 아니다.**
[원본과 실측 요약](fixtures/observations/realistic-local-20260910/README.md)에
정상·장애·복원과 과거 실패 실행을 보존했다. 모델에는 실제 UTC 기준의 정상·사고
관측만 제공하고 복원 후 정보는 제외한다. 초기 합성 관측도
[초안 이력](fixtures/historical/illustrative-drafts/)에 보존했다.

| ID | 기대 유형 | 원인과 복원 |
|---|---|---|
| `pool-config-regression` | `unsupported` | 실제 풀 설정 축소 → 원래 설정 복원 |
| `query-amplification` | `slow-query` | 일괄 조회의 행별 재조회 → 원래 이미지 복원 |
| `maintenance-transaction-lock` | `unsupported` | 소유 정비 트랜잭션의 쓰기 차단 → 해당 작업만 롤백 |
| `exception-session-cleanup` | `db-leak` | 예외 경로 세션 반환 누락 → 정상 이미지 복귀·결함 태스크 종료 |

중립 관측 ID, 제공된 시각·단위·리소스, 원본 형태의 레코드와 독립적인 경쟁 원인
반증을 사용한다. 풀 부족·락을 누수로 바꾸어 채점하지 않는다. 모든 사례는 원인
확정·필수 산출물·안전한 실행 절차를 요구하며 평가기는 변경하지 않았다.
실측 요청 항목과 관측 대응표는
[측정 인계](../docs/demo/scenario-evidence-handoff.md)에 있다.
[브라우저용 설명](../docs/demo/realistic-scenarios.html)은 별도 빌드 없이 열린다.

공용 조회 증상은 `${ns}-Healthcare-PatientVitalsQueryLatency`, 메트릭
`PatientVitalsQueryDuration`, `Healthcare/Sensor`, `ServiceName=healthcare-sensor-app`,
Milliseconds, Average, 60초×2 평가다. 배포 기본 튜닝 값 500ms는 검증된 임계치가
아니다. 이번 로컬 조회는 정상·결함·복원 모두 개별 500ms 미만이므로 알람 발동을
주장하지 않고 카탈로그 threshold는 생략한다. `SOURCE_REVISION=r1/r2/r3`는
빌드할 소스를 선택하며, 관측은 측정된 source manifest의 실제 해시와 연결한다.

원래 네 시나리오와 정규화 fixture는
[`fixtures/historical/original-four/`](fixtures/historical/original-four/)에 바이트
그대로 보존했다. `tests/results/model/efficiency-20260909T145305Z-b05412`의 기존
실패·부분 결과는 수정하지 않았다. 새 모델 결과 fixture는 없다.
`tests/baseline/`도 수정하지 않았다. **현재 `eval:offline`은 결과 부재로 실패하고,
기존 기준선 digest 검사는 변경 승인을 요구하며 실패한다.** 테스트용 fake engine이나
메모리 내 구조 검증용 객체는 실제 모델 결과·승인 자료가 아니다.

네 입력은 `model-eval`만 선언한다. 배포 제어와 복원 경로를 검증한 뒤 해당 사례에만
`deployed-e2e`를 추가한다. 로컬 DB 재현, 제공 관측 실모델 평가, AWS 배포 E2E의
결과는 각각 기록하며 서로의 성공을 대신하지 않는다.

## 선택 알람 메타데이터

`alarm`은 `name`, `metric`, `stateReason` 외에 `stateChangeTime`, `region`,
`namespace`, `dimensions`(이름→값 객체), `statistic`, `period`(초), `threshold`,
`comparisonOperator`, `evaluationPeriods`, `datapointsToAlarm`, `treatMissingData`,
`arn`을 제공할 수 있다. 숫자 0과 빈 차원 객체도 보존한다. 생략/null 값으로
관측이나 평가 조건을 만들어내지 않는다. `datapointsToAlarm`을 평가 기간 수에서
추정하지 않는다.

Headless Codex는 기존 `AlarmContext`의 선택 필드로 전달한다. 두 엔진은 공용
프롬프트/DTO가 별도로 표시하지 않는 항목도 읽을 수 있도록 제공된 메타데이터를
알람 사유에 함께 보존한다. 운영 DTO·프롬프트 기본값은 유지한다. `model-eval`에서는 별도 원본 메타데이터를
최종 프롬프트까지 전달하여 누락·null·빈 값은 `not provided`로 표시한다.
Strands도 ARN 없이 제공된 `region`을 평가 프롬프트에 그대로 표시한다. 런타임의
AWS 실행 리전을 관측된 사고 리전으로 채우거나 없는 ARN을 생성하지 않는다.

Strands의 payload `StateChangeTime`은 실행마다 새 세션을 만드는 현재 시각이다.
시나리오의 `stateChangeTime`은 원본 알람 메타데이터에 따로 남고 실제 사고 구간은
각 관측의 `summary`에도 명시한다. 원본에 구간이 없으면 null과 누락 사유를 보존한다. 새 세션 시각을 관측 시각으로 해석하지 않는다.
평가자의 `expectation`은 모델 입력으로 전달하지 않는다.

## 로컬 검증

```bash
pnpm setup:test
pnpm verify
```

`pnpm verify`는 패키지 테스트, 프롬프트·도구 계약, 공통 RCA 시나리오와
승인된 입력 digest 기준선을 외부 AWS·모델 호출 없이 검사한다.

## 실모델 계약 평가

실모델 계약 평가는 두 엔진의 실행 어댑터와 AWS 자격 증명을 명시적으로 전달할
때만 실행된다.

```bash
export AWS_PROFILE=rca-dev
export AWS_REGION=us-east-1
export CODEX_MODEL=global.openai.gpt-5.6-sol
export CODEX_REASONING_EFFORT=high
export CODEX_MODEL_PROVIDER=amazon-bedrock-runtime
export RCA_EVAL_DEPLOYED_CODEX_MODEL=global.openai.gpt-5.6-sol
export RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT=high
export RCA_EVAL_DEPLOYED_CODEX_PROVIDER=amazon-bedrock-runtime
export RCA_EVAL_STRANDS_COMMAND='["uv","run","--project","packages/agent","rca-agent-eval","{scenario}"]'
export RCA_EVAL_HEADLESS_CODEX_COMMAND='["uv","run","--project","packages/headless-codex","headless-codex-eval","{scenario}"]'
pnpm eval:model
```

두 어댑터는 `executionModes`에 `model-eval`이 선언된 시나리오만 받으며, 시나리오가
제공한 `observations`를 초기 컨텍스트에 넣어 실제 모델의 구조화된 RCA 결과를 평가한다.
따라서 이 경로는 배포 E2E가 아니며, SQS 전달·SNS 구독·재전달·실제 증거 소스에서
관측을 찾아내는 능력을 증명하지 않는다.

Strands 어댑터는 SQS 소비 루프를 거치지 않고 공용 분석 파이프라인을 직접
호출한다. DynamoDB 세션 테이블, S3 보고서 버킷, Bedrock, CloudWatch·CloudTrail
조회 권한이 필요하다. **`DYNAMODB_TABLE_NAME`을 반드시 설정한다** — 이 값이 없으면
세션 스토어가 비활성이 되고, 그 상태가 활성 인시던트 경합과 같은 로그·같은 반환값으로
나타나 "다른 실행이 이미 처리 중"으로 오진하게 된다. 이 어댑터는 세션에 기록된
상태를 완료 판정의 권위로 삼으므로 스토어 없이는 결과를 낼 수 없다.

```bash
export DYNAMODB_TABLE_NAME=<세션 테이블>
export S3_REPORT_BUCKET=<보고서 버킷>
export S3_EVIDENCE_BUCKET=<증거 버킷>
export S3_VECTOR_BUCKET_NAME=<벡터 버킷>
```

평가 실행은 이 테이블과 버킷에 세션·스팬·보고서·플레이북을 실제로 쓴다. 배포 환경의
리소스를 지정하면 운영 데이터와 섞이므로, 실행 후 남은 세션과 활성 인시던트 항목을
정리한다. 활성 인시던트를 남기면 같은 알람의 실제 장애가 억제될 수 있다.

headless-codex 어댑터는 배포된 것과 같은 하네스를 로컬에서 한 번 실행한다. Codex
CLI와 하네스가 참조하는 MCP 서버 실행기가 로컬에 설치되어 있어야 하고,
Bedrock 및 CloudWatch·CloudTrail 조회 권한이 필요하다.
배포 전 확인한 Headless Codex task definition의 모델, reasoning effort, provider를
`RCA_EVAL_DEPLOYED_CODEX_*` 변수에 명시한다. 하네스는 로컬 값과 배포 값의 정확한
일치를 실행 전에 검사하고 보고서의 `modelContract`에 기록한다. 결정적 테스트에서
AWS를 자동 조회하지 않는다.
호출별 기본 상한은 배포 실행 상한과 같은 60분이며,
`RCA_EVAL_TIMEOUT_MS`로 더 짧게 설정할 수 있다.

`pnpm eval:model`은 기본적으로 두 엔진을 모두 실행하며, 실행할 엔진의 command만
요구한다. `--engine`으로 엔진을 좁히면 그 엔진만 실행하고 나머지 엔진의 command와
Headless Codex 모델 패리티 변수는 검사하지 않는다.

```bash
# 한 엔진만 실행 (진단·부분 확인용)
pnpm eval:model --engine strands

# 남은 엔진을 같은 회차에 이어 실행 — 앞선 결과를 재사용해 회차가 전수를 채운다
pnpm eval:model --engine headless-codex --results tests/results/model/<run-id>/results
```

한 엔진의 회차가 수십 분을 쓰므로, 다른 엔진의 실패나 환경 설정 오류 때문에 이미
통과한 회차를 버리지 않도록 실행을 나눌 수 있다. 보고서의 `engines`,
`enginesRun`, `enginesReused`, `enginesComplete`가 그 회차가 실제로 무엇을
측정했는지 기록한다. **기준선 승인은 두 엔진 전수가 모였을 때만 성립한다** —
부분 실행은 진단 수단이고 승인 근거가 아니다.

각 command는 JSON 문자열 배열이다. `{scenario}`는 시나리오 파일의 절대 경로,
`{scenarioId}`는 시나리오 ID로 치환된다. `{scenario}`를 사용하지 않으면 시나리오
JSON이 표준 입력으로 전달된다.

엔진 어댑터는 표준 출력에 로그 없이 정규화된 결과 JSON 한 개만 기록해야 한다.
필수 필드는 `schemaVersion`, `scenarioId`, `engine`, `rootCause`,
`rootCauseConfirmed`, `rootFaultType`, `rootCauseEvidenceIds`, `evidenceIds`,
`artifacts`, `competingCauseJudgments`, `remediation.summary`,
`remediation.available`, `remediation.verificationStatus`,
`remediation.executionSteps`, `remediation.safe`, `remediation.unsafeSteps`,
4개 `remediation.safeguards`다. `executionSteps`의 각 항목은 고유한 `stepId`와
비어 있지 않은 `intent`, `action`, `successCriteria`를 가진다.
`rootCause`와 `remediation.summary`는 사람이 읽는 설명이며 키워드로 채점하지 않는다.

실모델 결과와 보고서는 기본적으로 `tests/results/model/`에 저장되며 Git에서
제외된다. 실제 AWS 리소스와 모델을 호출하는 어댑터는 배포 환경의 알람·리소스
매핑을 소유해야 하며 일반 CI에서는 실행하지 않는다.

## 배포 E2E

`executionModes`에 `deployed-e2e`가 선언된 시나리오만 실제 장애 주입 대상으로
사용한다. 이 계층은 제공된 `observations`를 엔진에 전달하지 않고, CloudWatch 증상
알람에서 SNS/SQS를 거쳐 두 배포 엔진이 새 세션을 만들고 실제 증거를 조회하는지
확인한다. 주입부터 정리까지 같은 `RUN_ID`를 사용하며 과거 세션은 증거로 재사용하지
않는다. 실행 절차는 [배포 E2E 런북](../docs/execution-live-e2e-runbook.md)을 따른다.

## 필수 평가 차원

| 차원                      | 통과 조건                                                                                                            |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `rootCauseIdentified`     | 허용된 `rootFaultType`, 필수 근본원인 증거, 시나리오가 요구하는 확정 상태                                            |
| `evidenceLinked`          | `requiredEvidenceIds`를 모두 인용                                                                                    |
| `artifactsComplete`       | `requiredArtifacts`를 모두 저장                                                                                      |
| `remediationSafe`         | available + safe + 빈 unsafeSteps + DRAFT + 4개 safeguard, 실행 가능한 절차가 필요하면 비어 있지 않은 executionSteps |
| `competingCausesRejected` | 기대 원인 집합과 정확히 일치하고, 각 원인이 자기 `requiredEvidenceIds`로 `rejected` 판정됨                           |

`competingCauses`는 선택 필드다. 정의하지 않은 시나리오는 이 차원을 자동
통과한다. 정의하면 각 항목은 `id`와 `requiredEvidenceIds`만 가지며, 원인 ID와 필수
증거 집합은 각각 고유해야 한다. 한 원인의 증거를 다른 원인에 인용하거나 전체 증거를
합쳐 필수 집합을 채우는 방식은 통과하지 않는다.

## 기준선 승인

검토한 실모델 결과가 구조 게이트를 통과한 경우에만 기준선을 명시적으로
갱신한다.

```bash
pnpm eval:approve --results tests/results/model/<run-id>/results
```

승인은 **선언된 모든 엔진의 모든 시나리오 결과**를 요구한다. 한 엔진만 담긴 결과
디렉터리로 승인하려 하면 어느 엔진이 빠졌는지와 함께 거부된다 — 기준선의 목적이 두
엔진을 같은 품질 계약으로 비교하는 것이므로, 한 엔진만으로 승인하면 비교 근거가 없는
값이 기준선의 이름을 갖는다. 엔진을 나눠 실행했다면 같은 결과 디렉터리에 나머지 엔진을
이어 실행한 뒤 승인한다.

프롬프트, skill, MCP 또는 시나리오 입력이 변경되면 digest 게이트가 실패한다.
평가 정책과 정규화 결과 fixture도 digest 입력이다. 기준선은 의미 점수를 저장하지
않으며, 변경 결과를 검토하지 않은 상태에서 fixture만으로 digest를 갱신하지 않는다.
부분 실행 사이에 계약 입력을 바꾸면 승인 시점의 digest가 달라져 거부된다.
