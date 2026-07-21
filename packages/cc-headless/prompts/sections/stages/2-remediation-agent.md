## 2단계: 조건부 Remediation 전문 에이전트

최신 validation의 `confirmed`가 비어 있지 않을 때만 Agent tool로
`remediation-specialist`를 호출하고 RCA 응답 전체를 전달한다.

- confirmed 항목과 참조 hypothesis의 구조화된 `fault_type`을 함께 전달한다.
- 미확정이면 호출하지 않고 `status=NOT_ATTEMPTED`, `reason=unconfirmed root cause`로
  Report 입력을 구성한다.
- 전문 에이전트가 `BLOCKED` 또는 `FAILED`를 반환해도 파이프라인을 중단하지 않는다.
- 임의 HTTP, Bash, ECS update 또는 다른 fallback을 시도하지 않는다.
