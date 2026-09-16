# RCA Evaluation Harness

루트 하네스는 오프라인 계약 테스트, fixture 구조 회귀, 실모델 계약 평가를
분리한다. 실제 배포 이벤트 전달과 증거 탐색은 별도의 배포 E2E로 검증한다.

## 단일 데모 시나리오

활성 카탈로그는 `tests/scenarios/write-column-regression.json` 하나다. 정상 `v1`의
INSERT(행 저장 SQL)는 `timestamp`를 사용하며, 결함 `v2`는 같은 소스에서
`sampled_at` 참조 한 곳만 바꾼다. 실제 PostgreSQL의 존재하지 않는 컬럼 오류
SQLSTATE `42703`을 대상으로 한다. 기존 평가 유형에는 컬럼 회귀가 없으므로
`acceptedRootFaultTypes`는 `unsupported`다. 원인 확정·증거·산출물·안전한 실행
절차의 기존 기준을 그대로 적용하며 별도 분류기나 임계치를 추가하지 않는다.

활성 fixture는 헬스케어 작업이 제공한 `20260915-final-observer` 실제 PostgreSQL
증거를 사용한다. 정상 6행 저장, 장애 구간 0행 추가와 실제 `42703`, 동일한 실제
스키마 및 정상 조회·헬스가 원본에 기록됐다. 정상·결함 INSERT 소스 바이트의
SHA-256을 원본 지문과 대조했다. 로컬 EMF(메트릭 형식 로그) 출력은 CloudWatch에서
조회한 1분 지표 구간이 아니며, 알람 외곽 입력도 여전히 합성 예시다.

원본·소스 바이트·대응표는 `scenarios/captures/native-column-20260915/`에 보존했다.
`projectWriteColumnCaptures`는 실제 정상·사고 시각 안의 사실만 추출하며 복원 후
관측과 운영자의 통과 판정은 제외한다. 초기 합성 계약 입력은
`scenarios/history/synthetic/`에 남긴다. 실제 AWS·RCA·승인 실행 성공을 주장하지
않으며 전체 코드 통합 후 입력 지문 동기화는 부모 작업에서 수행한다.

직전 네 사례는 `tests/scenarios/history/`에 바이트 그대로 보존했다.
`fixtures/observations/`, `fixtures/historical/`, `baseline/history/`와 기존 승인
자료도 보존한다. 과거 관측 재현·보안 검사는 이력 경로를 명시해 실행한다.
과거 결과는 새 사례의 모델 평가 통과 근거가 될 수 없다.

단일 제어는 `scripts/run_realistic_demo.py`의 `plan / apply / status / restore`다.
`inject_deployment_fault.py`와 `run_deployed_e2e.py`도 같은 CLI로 위임한다.
이전 플래그·정비 잠금·red-herring 명령은 폐기됐다.
[실행 안내](../scripts/run_realistic_demo.html)를 따른다. AWS 조작은 운영자가
담당하며 루트 계약 검사는 AWS CLI를 모의 구현한다.

각 실행은 새 RunId와 공유 저널 디렉터리를 사용한다. 운영자는 준비 3회와 본실행
10회의 독립 RunId를 시작 전에 고정하고 실패·중단을 포함해 모두 기록한다.
정상 기준은 실제 두 완결된 1분 구간의 양수 시도·명시적 0 실패 및 실제 로그로
검증한다. S3 `baselines/<run_id>/normal.json`을 `IfNoneMatch=*`로 생성하고
다운로드한 UTF-8 정규 JSON의 SHA-256을 확인한 뒤 알람 설명에 참조를 연결한다.
알람의 임계치·차원·평가 조건은 유지하고 cleanup에서 원래 설명을 복원한다.

runner는 실행 승인을 보내지 않는다. 사용자 또는 위임받은 부모 운영자가 일반
대시보드 API로 승인한다. `recoveryVerified`는 환경 정리 결과이며 RCA 확정이나
`RESOLVED`를 의미하지 않는다. 실제 전체 흐름은 원인 확정 → 고정 런북 → 사용자
승인 → 실행 워커 롤백 → 배포 수렴 → 실제 저장 회복 → `RESOLVED`를 별도 확인한다.

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

`pnpm verify`는 패키지 테스트, 프롬프트·도구 계약, 현재 입력 지문, 과거 fixture의
구조 회귀와 승인된 모델 결과를 외부 AWS·모델 호출 없이 검사한다.
과거 시나리오·결과는 서로 짝지어 검사하고, 현재 입력 지문 검사는 별도로 수행한다.
`eval:offline`도 여전히 필수이므로 모델 승인 대기 상태에서는 전체 검증이 실패한다.

코드·시나리오의 변경 내용을 검토한 뒤 현재 입력 지문만 갱신하려면:

```bash
pnpm eval:sync-inputs
```

이 명령은 모델을 호출하거나 결과를 만들지 않는다. 입력이 달라졌으면 이전 기준선의
원본을 이력에 저장하고, 현재 기준선을 `schemaVersion: 3`, `status: pending`,
`approvedAt: null`로 기록한다. 입력이 같으면 시각과 승인 상태도 바꾸지 않는다.
따라서 이미 승인된 같은 입력을 재기록해 승인을 불필요하게 무효화하지 않는다.

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
승인한다. 현재 카탈로그의 검토된 정규화 결과를 `tests/fixtures/results/`에
보존한 뒤 그 결과로 승인해야 일반 CI도 같은 결과와 입력 지문을 검사한다.

```bash
pnpm eval:approve --results tests/fixtures/results
```

승인은 **선언된 모든 엔진의 모든 시나리오 결과**를 요구한다. 한 엔진만 담긴 결과
디렉터리로 승인하려 하면 어느 엔진이 빠졌는지와 함께 거부된다 — 기준선의 목적이 두
엔진을 같은 품질 계약으로 비교하는 것이므로, 한 엔진만으로 승인하면 비교 근거가 없는
값이 기준선의 이름을 갖는다. 엔진을 나눠 실행했다면 같은 결과 디렉터리에 나머지 엔진을
이어 실행한 뒤 승인한다.

승인 명령은 기준선을 `approved`로 기록하고 실제 승인 시각을 저장한다.
이전 schema v2 승인 기록도 계속 읽을 수 있다.
프롬프트, skill, MCP 또는 시나리오 입력이 변경되면 입력 일치 검사가 실패한다.
평가 정책과 정규화 결과 fixture도 digest 입력이다. 기준선은 의미 점수를 저장하지
않으며, 변경 결과를 검토하지 않은 상태에서 fixture만으로 digest를 갱신하지 않는다.
입력 변경 후 `eval:sync-inputs`로 현재 계약을 기록해도 모델 승인 상태는
`pending`이다. 전수 결과의 검토·승인 전에는 모델 평가 게이트를 통과하지 못한다.
