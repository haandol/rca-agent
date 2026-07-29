# RCA Evaluation Harness

루트 하네스는 오프라인 계약 테스트와 명시적 라이브 평가를 분리한다.

## 로컬 검증

```bash
pnpm setup:test
pnpm verify
```

`pnpm verify`는 패키지 테스트, 프롬프트·도구 계약, 공통 RCA 시나리오와
승인된 의미 기준선을 외부 AWS·모델 호출 없이 검사한다.

## 라이브 평가

라이브 평가는 두 엔진의 실행 어댑터와 AWS 자격 증명을 명시적으로 전달할 때만
실행된다.

```bash
export AWS_PROFILE=rca-dev
export AWS_REGION=us-east-1
export RCA_EVAL_STRANDS_COMMAND='["uv","run","--project","packages/agent","rca-agent-eval","{scenario}"]'
export RCA_EVAL_CC_HEADLESS_COMMAND='["uv","run","--project","packages/cc-headless","cc-headless-eval","{scenario}"]'
pnpm eval:live
```

두 어댑터 모두 배포된 것과 같은 분석 경로를 로컬에서 한 번 실행한다. Strands
어댑터는 SQS 소비 루프를 거치지 않고 운영 파이프라인을 직접 호출하므로, 큐·구독·
재전달 동작은 이 경로로 검증되지 않는다. Strands 어댑터는 DynamoDB 세션 테이블,
S3 보고서 버킷, Bedrock, CloudWatch·CloudTrail 조회 권한이 필요하다.

cc-headless 어댑터는 배포된 것과 같은 하네스를 로컬에서 한 번 실행한다. Claude
Code CLI와 하네스가 참조하는 MCP 서버 실행기가 로컬에 설치되어 있어야 하고,
Bedrock 및 CloudWatch·CloudTrail 조회 권한이 필요하다.

`pnpm eval:live`는 두 엔진의 command 를 모두 요구한다.

각 command는 JSON 문자열 배열이다. `{scenario}`는 시나리오 파일의 절대 경로,
`{scenarioId}`는 시나리오 ID로 치환된다. `{scenario}`를 사용하지 않으면 시나리오
JSON이 표준 입력으로 전달된다.

엔진 어댑터는 표준 출력에 로그 없이 정규화된 결과 JSON 한 개만 기록해야 한다.
필수 필드는 `schemaVersion`, `scenarioId`, `engine`, `rootCause`,
`evidenceIds`, `artifacts`, `remediation.summary`, `remediation.safe`,
`remediation.safeguards.preconditions`, `approval`, `rollback`,
`verification`이다.

라이브 결과와 보고서는 기본적으로 `tests/results/live/`에 저장되며 Git에서
제외된다. 실제 AWS 리소스와 모델을 호출하는 어댑터는 배포 환경의 알람·리소스
매핑을 소유해야 하며 일반 CI에서는 실행하지 않는다.

## 필수 평가 차원

| 차원 | 통과 조건 |
|------|----------|
| `rootCauseIdentified` | 근본원인 표현이 `rootCauseTermGroups`의 모든 그룹을 만족 |
| `evidenceLinked` | `requiredEvidenceIds`를 모두 인용 |
| `artifactsComplete` | `requiredArtifacts`를 모두 저장 |
| `remediationSafe` | 안전 선언 + `remediationTermGroups` 충족 + 4개 safeguard 모두 기술 |
| `competingCausesRejected` | `rejectedCauseTermGroups`의 어떤 그룹도 근본원인에 등장하지 않음 |

`rejectedCauseTermGroups`는 선택 필드다. 정의하지 않은 시나리오는 이 차원을 자동
통과한다. 정의하면 경쟁 가설을 근본원인으로 지목한 결과가 필수 게이트에서 실패하므로,
정답을 맞히는 것과 오답을 배제하는 것을 함께 측정할 수 있다.

## 기준선 승인

검토한 라이브 결과가 기존 필수 게이트를 통과한 경우에만 기준선을 명시적으로
갱신한다.

```bash
pnpm eval:approve --results tests/results/live/<run-id>/results
```

프롬프트, skill, MCP 또는 시나리오 입력이 변경되면 digest 게이트가 실패한다.
평가 정책과 정규화 결과 fixture도 digest 입력이다. 변경 결과를 검토하지 않은
상태에서 fixture만으로 기준선을 갱신하지 않는다.
