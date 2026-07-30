#### playbook.json

플레이북은 **실행의 근거**다. 사용자가 이 절차를 승인하면 별도 실행 에이전트가
`execution_steps`를 순서대로 수행한다. 따라서 사람이 읽는 서술과 실행 가능한
구조화 절차를 함께 담는다.

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
  "execution_steps": [
    {
      "step_id": "step-1",
      "intent": "이 단계가 무엇을 달성하려는지",
      "action": "수행할 작업을 자연어로 — 대상 리소스와 조작 내용을 명시",
      "success_criteria": "성공을 판정할 관측 기준 — 어떤 지표가 어떤 값이 되면 성공인지"
    }
  ],
  "temporary_mitigation": "즉시 증상을 완화하는 조치와 한계",
  "permanent_remediation": "영구 개선 권고",
  "escalation_criteria": "실행 실패 또는 관측 지연 시 에스컬레이션 기준",
  "prevention_measures": ["재발 방지 항목"],
  "tags": ["lowercase-kebab-case"],
  "verification_status": "DRAFT",
  "summary": "플레이북 생성 완료",
  "output_summary": "장애 유형과 실행 절차 요약"
}
```

### execution_steps 규칙

- **`step_id`는 안정적인 식별자**다. 실행 증거가 어느 단계에서 실패했는지 지목하고
  회고가 그 단계를 교정하므로, 순서를 바꾸더라도 기존 식별자를 재사용하지 않는다.
- **`action`은 자연어로 쓴다.** 명령 문자열을 박아 넣지 않는다. 리소스 식별자와
  리전은 실행 시점 알람 컨텍스트에서 결정되며, 절차에 고정하면 같은 유형의 다른
  리소스 장애에 재사용할 수 없다. 단 **어느 리소스를 조작하는지는 반드시 명시**한다.
- **`success_criteria`는 관측 가능해야 한다.** "정상화됨" 같은 서술 대신 어떤 지표가
  어떤 범위로 돌아오면 성공인지 쓴다. 이 기준이 없으면 실행 에이전트가 이슈 해소를
  판정할 수 없다.
- **되돌릴 수 없는 조치를 담지 않는다.** 리소스·데이터·스냅샷 삭제, 인스턴스 종료,
  자격 증명 회수, 계정·조직 수준 변경은 실행 계층이 거부하므로 그 단계는 수동 조치로
  남는다. 그런 조치가 필요하면 `permanent_remediation`에 권고로 쓴다.
- 확정 근본원인이 없으면 `execution_steps`를 비운다. 미확정 원인에 대한 추측 절차는
  실행 근거가 될 수 없다.

`verification_status`는 항상 `DRAFT`로 쓴다. 이 플레이북은 아직 실행되지 않았으므로
절차의 정확성이 검증되지 않았다. 실행과 회고를 거친 뒤 서버가 이 값을 갱신한다.

위 스키마의 모든 키는 필수다. 하나라도 없거나 빈 문자열이면 저장이 거부되고
플레이북은 기록되지 않는다. 특히 `severity_criteria`, `escalation_criteria`,
`symptom_pattern`을 생략하지 않는다. 저장 도구가 `ok: false`를 반환하면 지적된
필드를 채워 같은 파일을 다시 저장한다.

실행 결과와 정상화 여부는 이 플레이북에 쓰지 않는다. 실행은 사용자 승인 뒤에
일어나므로 작성 시점에는 알 수 없다.
