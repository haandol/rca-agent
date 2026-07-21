#### playbook.json

플레이북은 **별도 Remediation Agent 또는 승인된 오퍼레이터가 검토하고 실행하는
절차서**이다. CC Headless는 문서화만 하며 변경을 실행하지 않는다. 모든 단계는
"어떤 액션을 제안한다 → 어떤 사전조건·승인이 필요하다 → 어떤 조건으로 롤백하고
검증한다"가 명확해야 한다.

```json
{
  "stage": "PLAYBOOK",
  "playbook_id": "UUID",
  "failure_type": "장애 유형 한 줄 분류 (예: RDS 커넥션 풀 소진)",
  "symptom_pattern": "이 장애를 시사하는 알람/메트릭 패턴 (구체적 임계치 포함)",
  "severity_criteria": "심각도 판정 기준 (low/medium/high/critical 각각 어떤 수치·영향 범위일 때인지)",
  "related_metrics": [
    "AWS/RDS DatabaseConnections (DBInstanceIdentifier=...) — 정상 <N, 장애 시 >M"
  ],
  "verification_steps": [
    "### 1. 알람 메트릭 정상화 판정\n- **검증 메트릭**: AWS/RDS DatabaseConnections (DBInstanceIdentifier=<id>)\n- **조회 후보**: cloudwatch MCP `get_metric_data`\n- **관측 조건**: 별도 실행 주체가 변경 성공을 확인한 뒤, period=60, 연속 3개 데이터포인트\n- **기준값**: 정상 50 이하, 알람 임계치 200\n- **Pass 판정**: 연속 3개 값이 200 미만이고 감소 추세\n- **Fail 판정**: 200 이상 지속 또는 증가 시 롤백 검토 후 DBA 온콜 에스컬레이션",
    "### 2. 서비스 오류율 동반 판정\n- **검증 메트릭**: AWS/ApplicationELB HTTPCode_Target_5XX_Count (TargetGroup=<id>)\n- **조회 후보**: cloudwatch MCP `get_metric_data`\n- **관측 조건**: 동일 관측 구간, period=60\n- **기준값**: 장애 전 baseline < 1%\n- **Pass 판정**: 오류율이 baseline 범위\n- **Fail 판정**: 오류율 > 5%가 2개 구간 지속되면 롤백"
  ],
  "temporary_mitigation": "### 복구 권고 후보\n- **제안할 액션**: 별도 실행 주체가 `POST http://<HEALTHCARE_SERVICE_HOST>:8000/fault/db-leak/reset` 검토\n- **사전조건**: 확정 가설 신뢰도 >= 0.8, 대상 환경과 fault 상태 확인, 정상 트래픽 경로와 분리 확인\n- **승인 필요**: 예 — 서비스 온콜\n- **기대 결과**: HTTP 200 및 reset 상태\n- **롤백 조건**: 5XX 오류율 > 5%가 2개 구간 지속되거나 DatabaseConnections 증가\n- **실행 상태**: CC Headless 미실행\n- **대체 후보**: 위 액션이 부적합하거나 별도 실행에서 실패한 경우 승인 후 `UpdateService(forceNewDeployment=true)` 검토",
  "permanent_remediation": "### 영구 개선\n1. 애플리케이션 커넥션 풀 설정 점검 (max=N, idle_timeout, leak_detection_threshold)\n2. `try-with-resources` / context manager로 커넥션 반납 보장\n3. RDS Performance Insights에서 Top wait events가 `client-read` 계열인지 확인\n4. CI에 leak detector 통합",
  "escalation_criteria": "임시 조치 2회 실패 또는 DatabaseConnections가 10분 내 정상화 안 될 때 DBA 온콜 호출. 데이터 손상 의심 시 SEV-1 선언.",
  "prevention_measures": [
    "CloudWatch 알람: DatabaseConnections > 임계치의 70%에서 경보",
    "배포 파이프라인에 커넥션 풀 누수 테스트 추가",
    "SLO: 커넥션 사용률 < 80% 유지"
  ],
  "tags": ["rds", "connection-pool", "db-leak"],
  "summary": "플레이북 생성 완료",
  "output_summary": "playbook_id=UUID, 장애유형=RDS 커넥션 풀 소진"
}
```

**플레이북 필드 필수 규칙**:
- `temporary_mitigation`은 `제안할 액션` → `사전조건` → `승인 필요` → `기대 결과` → `롤백 조건` → `실행 상태` 순서를 따른다.
- `verification_steps`는 `검증 메트릭` → `조회 후보` → `관측 조건` → `기준값` → `Pass 판정` → `Fail 판정` 순서를 따른다.
- 변경 액션은 별도 실행 주체를 위한 후보로만 기록하고 `CC Headless 미실행`을 명시한다.
- 조회 후보는 MCP 도구와 구체 파라미터를 포함한다. 모호한 한글 서술("연결 수 확인") 금지.
- 타임스탬프·임계치·리소스 ID 등 수치는 placeholder(`<id>`)를 써서 재사용 가능하게 둔다. 구체 값은 `related_metrics`에 normal/abnormal 범위로 기록한다.
- 각 단계는 별도 실행 주체가 **독립적으로 검토 가능**해야 한다 (직전 단계의 암묵적 상태 가정 금지).

**JSON은 반드시 valid해야 한다. 파싱 실패 시 해당 단계가 에러로 기록된다.**
