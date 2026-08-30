# RCA Evaluation Harness

루트 하네스는 오프라인 계약 테스트, fixture 구조 회귀, 실모델 계약 평가를
분리한다. 실제 배포 이벤트 전달과 증거 탐색은 별도의 배포 E2E로 검증한다.

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
export RCA_EVAL_CODEX_HEADLESS_COMMAND='["uv","run","--project","packages/codex-headless","codex-headless-eval","{scenario}"]'
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

codex-headless 어댑터는 배포된 것과 같은 하네스를 로컬에서 한 번 실행한다. Codex
CLI와 하네스가 참조하는 MCP 서버 실행기가 로컬에 설치되어 있어야 하고,
Bedrock 및 CloudWatch·CloudTrail 조회 권한이 필요하다.
배포 전 확인한 Codex Headless task definition의 모델, reasoning effort, provider를
`RCA_EVAL_DEPLOYED_CODEX_*` 변수에 명시한다. 하네스는 로컬 값과 배포 값의 정확한
일치를 실행 전에 검사하고 보고서의 `modelContract`에 기록한다. 결정적 테스트에서
AWS를 자동 조회하지 않는다.
호출별 기본 상한은 배포 실행 상한과 같은 60분이며,
`RCA_EVAL_TIMEOUT_MS`로 더 짧게 설정할 수 있다.

`pnpm eval:model`은 기본적으로 두 엔진을 모두 실행하며, 실행할 엔진의 command만
요구한다. `--engine`으로 엔진을 좁히면 그 엔진만 실행하고 나머지 엔진의 command와
Codex Headless 모델 패리티 변수는 검사하지 않는다.

```bash
# 한 엔진만 실행 (진단·부분 확인용)
pnpm eval:model --engine strands

# 남은 엔진을 같은 회차에 이어 실행 — 앞선 결과를 재사용해 회차가 전수를 채운다
pnpm eval:model --engine codex-headless --results tests/results/model/<run-id>/results
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
