#### playbook.json

플레이북은 **사람 읽기용 문서가 아니라 다음 장애 발생 시 에이전트·오퍼레이터가 기계적으로 따라 실행하는 절차서**이다. 모든 단계는 "어떤 도구/명령을 호출한다 → 어떤 출력을 기대한다 → 어떤 조건으로 판정한다"가 명확해야 한다.

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
    "### 1. 알람 메트릭 재조회\n- **Tool**: cloudwatch MCP `get_metric_data`\n- **Params**: `{namespace: 'AWS/RDS', metric: 'DatabaseConnections', dimensions: {DBInstanceIdentifier: '<id>'}, period: 60, range: '-30m..now'}`\n- **Expected**: 정상 시 50 이하, 장애 시 200 이상\n- **Pass**: 최근 3개 데이터포인트 < 임계치 → 장애 아님, 다른 플레이북 확인\n- **Fail → 다음 단계**",
    "### 2. 변경 이력 조회\n- **Tool**: cloudtrail MCP `lookup_events`\n- **Params**: `{event_names: ['UpdateService','RegisterTaskDefinition'], time_range: '알람 -1h..알람', resource: '<service>'}`\n- **Expected**: 배포 이벤트 0-1건\n- **Pass 조건**: 이벤트 1건 이상이면 배포 상관 가설 우선, 없으면 커넥션 누수 가설"
  ],
  "temporary_mitigation": "### 즉각 조치 (목표: 5분 내 메트릭 정상화)\n1. **리셋 API 호출**\n   - `POST http://<HEALTHCARE_SERVICE_HOST>:8000/fault/db-leak/reset`\n   - **기대 응답**: HTTP 200 `{\"status\":\"reset\"}`\n2. **30초 대기 후 메트릭 확인** (1번 verification step 반복)\n3. **실패 시**: ECS 강제 배포 (`aws ecs update-service --force-new-deployment ...`)",
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
- `verification_steps`, `temporary_mitigation`, `permanent_remediation` 각 항목은 위 예시처럼 `### 제목` → `Tool/Command/Params` → `Expected` → `Pass/Fail 조건` 순서를 따르는 **마크다운 블록**으로 작성한다.
- 명령은 MCP 도구명, AWS CLI 명령, HTTP 메서드+엔드포인트, CloudWatch Logs Insights 쿼리 중 하나를 **그대로 복사해 실행 가능**한 형태로 기록한다. 모호한 한글 서술("연결 수 확인") 금지.
- 타임스탬프·임계치·리소스 ID 등 수치는 placeholder(`<id>`)를 써서 재사용 가능하게 둔다. 구체 값은 `related_metrics`에 normal/abnormal 범위로 기록한다.
- 각 단계는 **독립적으로 실행 가능**해야 한다 (직전 단계의 암묵적 상태 가정 금지).

**JSON은 반드시 valid해야 한다. 파싱 실패 시 해당 단계가 에러로 기록된다.**
