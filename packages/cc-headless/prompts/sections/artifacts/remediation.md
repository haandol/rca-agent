#### remediation.json

이 파일은 `execute_healthcare_reset` 도구가 서버에서 저장하며 에이전트가 직접
작성하거나 덮어쓰지 않는다.

```json
{
  "stage": "REMEDIATION",
  "status": "SUCCEEDED | FAILED | BLOCKED",
  "fault_type": "db-leak | high-cpu | high-memory | slow-query | unsupported",
  "endpoint_path": "/fault/db-leak/reset 또는 null",
  "validation_artifact": "validation-2.json",
  "confirmed_hypothesis_ids": ["UUID"],
  "summary": "실행 또는 차단 결과",
  "output_summary": "SUCCEEDED: 실행 결과",
  "verification": {
    "status": "NORMALIZED | FAILED | PENDING",
    "namespace": "AWS/RDS",
    "metric_name": "DatabaseConnections",
    "comparison_operator": "GreaterThanThreshold",
    "threshold": 30,
    "observed_value": 8,
    "observed_at": "ISO-8601 또는 null",
    "attempts": 2,
    "reason": "서버 측 bounded CloudWatch 검증 결과"
  }
}
```

`verification`은 reset 이후 서버가 직접 CloudWatch를 제한 횟수로 조회한 결과이다.
에이전트는 `NORMALIZED`, `FAILED`, `PENDING`을 수정하거나 추론으로 대체하지 않는다.
confirmed 원인이 allowlist에 매칭되지 않으면 서버는 `status=BLOCKED`,
`fault_type=unsupported`, `endpoint_path=null`을 기록한다. 미확정이면
`remediation.json`이 없어야 한다.
