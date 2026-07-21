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
export RCA_EVAL_STRANDS_COMMAND='["<strands-adapter>","{scenario}"]'
export RCA_EVAL_CC_HEADLESS_COMMAND='["<cc-adapter>","{scenario}"]'
pnpm eval:live
```

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

## 기준선 승인

검토한 라이브 결과가 기존 필수 게이트를 통과한 경우에만 기준선을 명시적으로
갱신한다.

```bash
pnpm eval:approve --results tests/results/live/<run-id>/results
```

프롬프트, skill, MCP 또는 시나리오 입력이 변경되면 digest 게이트가 실패한다.
평가 정책과 정규화 결과 fixture도 digest 입력이다. 변경 결과를 검토하지 않은
상태에서 fixture만으로 기준선을 갱신하지 않는다.
