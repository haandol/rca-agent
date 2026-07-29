#### playbook.json

```json
{
  "stage": "PLAYBOOK",
  "playbook_id": "UUID",
  "failure_type": "장애 유형",
  "symptom_pattern": "정량 임계치를 포함한 증상 패턴",
  "severity_criteria": "low/medium/high/critical 정량 기준",
  "related_metrics": ["namespace/metric/dimensions와 정상·장애 범위"],
  "verification_steps": [
    "검증 메트릭, 관측 조건, 기준값, Pass, Fail, 에스컬레이션을 포함한 단계"
  ],
  "temporary_mitigation": "이번 실행의 복구 결과와 후속 수동 조치",
  "permanent_remediation": "영구 개선 권고",
  "escalation_criteria": "복구 실패 또는 관측 지연 시 에스컬레이션 기준",
  "prevention_measures": ["재발 방지 항목"],
  "tags": ["lowercase-kebab-case"],
  "remediation_result": {
    "status": "remediation.json 의 status 복사 — NOT_ATTEMPTED | SUCCEEDED | FAILED | BLOCKED",
    "fault_type": "remediation.json 의 fault_type 복사 — db-leak 또는 unsupported",
    "endpoint_path": "remediation.json 의 endpoint_path 복사 — /fault/db-leak/reset 또는 null",
    "reason": "remediation.json 의 summary 를 글자 그대로 복사",
    "validation_artifact": "remediation.json 의 validation_artifact 복사 — validation-2.json",
    "verification": {
      "status": "remediation.json 의 verification.status 복사 — NORMALIZED | FAILED | PENDING",
      "reason": "remediation.json 의 verification.reason 을 글자 그대로 복사"
    }
  },
  "summary": "플레이북 생성 완료",
  "output_summary": "장애 유형과 복구 상태"
}
```

복구 성공을 서비스 정상화로 간주하지 않는다. `remediation.json.verification`을
그대로 복사한다. `PENDING`이면 관측값을 만들지 않고 후속 절차를 `관측 대기`로
기록한다.

`remediation_result`의 모든 값은 서버가 쓴 `remediation.json`에서 **글자 그대로
복사**한다. 요약하거나 다른 말로 바꾸거나 번역하지 않는다. 특히 `reason`은
`remediation.json`의 `summary` 문자열과 완전히 같아야 하며, 한 글자라도 다르면
저장이 거부된다. 복구가 실행되지 않아 `remediation.json`이 없으면 `status`를
`NOT_ATTEMPTED`로, 나머지 값을 `null`로 둔다.

위 스키마의 모든 키는 필수다. 하나라도 없거나 빈 문자열이면 저장이 거부되고
플레이북은 기록되지 않는다. 특히 `severity_criteria`, `escalation_criteria`,
`symptom_pattern`을 생략하지 않는다. 저장 도구가 `ok: false`를 반환하면 지적된
필드를 채워 같은 파일을 다시 저장한다.
