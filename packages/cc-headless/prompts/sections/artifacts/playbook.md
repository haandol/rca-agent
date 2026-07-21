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
    "status": "NOT_ATTEMPTED | SUCCEEDED | FAILED | BLOCKED",
    "fault_type": "db-leak 또는 unsupported",
    "endpoint_path": "/fault/db-leak/reset 또는 null",
    "reason": "실행 또는 차단 결과",
    "validation_artifact": "validation-2.json",
    "verification": {
      "status": "NORMALIZED | FAILED | PENDING",
      "reason": "서버 측 사후 검증 결과"
    }
  },
  "summary": "플레이북 생성 완료",
  "output_summary": "장애 유형과 복구 상태"
}
```

복구 성공을 서비스 정상화로 간주하지 않는다. `remediation.json.verification`을
그대로 복사한다. `PENDING`이면 관측값을 만들지 않고 후속 절차를 `관측 대기`로
기록한다.
