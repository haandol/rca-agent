## 3단계: Report 전문 에이전트

Agent tool로 `report-specialist`를 항상 호출한다. 다음을 그대로 전달한다.

- RCA 전문 에이전트의 전체 응답
- Remediation의 `NOT_ATTEMPTED`, `SUCCEEDED`, `FAILED`, `BLOCKED` 결과와 서버 측
  CloudWatch 검증 상태 `NORMALIZED`, `FAILED`, `PENDING`
- 원본 알람 상세

Report 에이전트가 `report.md`와 `playbook.json`을 모두 저장했는지 확인한다.
Remediation 실패를 이유로 보고서 생성을 재시도 없이 생략하지 않는다.
`remediation.json`의 status, fault type, endpoint, validation artifact, verification을
수정하거나 성공으로 재해석하지 않는다.
