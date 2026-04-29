## 9단계: 플레이북 생성 (직접 수행)

플레이북은 **유사 장애 재발 시 기계적으로(또는 다른 에이전트가) 실행할 수 있는 절차서**이다. 보고서와 달리 서술 설명·교훈·맥락은 배제하고, **호출할 도구·명령·기대 출력·판정 조건**만 담는다.

1. RCA 보고서에서 장애 유형, 증상 패턴, 이번에 실제 실행한 검증·복구 경로를 추출한다.
2. UUID `playbook_id`를 생성한다.
3. 각 절차 필드를 아래 구조로 마크다운 블록화하여 JSON에 넣는다.
4. `save_artifact("playbook.json", ...)` 으로 저장한다.

### 각 필드 작성 가이드

#### `failure_type` (string)
근본원인을 **한 줄 명사구**로. 예: `"RDS 커넥션 풀 소진"`, `"ECS 태스크 CPU 스트레스"`. 서술형 문장 금지.

#### `symptom_pattern` (string)
이 장애를 **자동 매칭**할 수 있는 패턴. 임계치·리소스 유형을 포함.
예: `"AWS/RDS DatabaseConnections이 정상 평균의 3배 이상으로 5분 이상 지속. 동시에 ReadLatency > 100ms."`

#### `severity_criteria` (string)
각 심각도 구간의 정량 기준. 예:
```
- critical: DatabaseConnections > max_connections의 95%, 서비스 전체 실패율 > 10%
- high: > 80%, 실패율 > 5%
- medium: > 60%, 실패율 > 1%
- low: > 50%, 실패율 < 1%
```

#### `related_metrics` (list[string])
이 장애 진단에 활용할 메트릭 목록. 각 항목에 `namespace/metric (dimensions) — normal: <N, abnormal: >M` 포맷.

#### `verification_steps` (list[string])
**각 항목은 독립 실행 가능한 마크다운 블록**. 템플릿:

```
### {N}. {단계 제목}
- **Tool**: {MCP 도구명 또는 AWS CLI 또는 HTTP}
- **Command/Query**:
  ```
  {복붙 실행 가능한 명령·쿼리·파라미터}
  ```
- **Expected**: {정상 범위 또는 기대 출력}
- **Pass**: {이 조건이면 다음 단계 건너뛰거나 장애 아님 판정}
- **Fail**: {이 조건이면 어디로 이동 — 다음 단계 / mitigation / escalation}
```

최소 3단계 권장: (1) 알람 메트릭 재확인 (2) 상관 메트릭·로그 수집 (3) 근본원인 특정.

#### `temporary_mitigation` (string)
**즉각 실행 절차**를 번호 매긴 단계로. 각 단계에 실제 명령(HTTP, AWS CLI) + 기대 응답 + 검증 방법.
마지막 단계는 **검증(메트릭 재조회)**이어야 한다.

Healthcare Service 엔드포인트 매핑:
- 커넥션 누수 → `POST /fault/db-leak/reset`
- 높은 CPU → `POST /fault/high-cpu/reset`
- 메모리 부족 → `POST /fault/high-memory/reset`
- 느린 쿼리 → `POST /fault/slow-query/reset`
- 매칭 없음 → `aws ecs update-service --force-new-deployment --cluster <c> --service <s>`

#### `permanent_remediation` (string)
영구 개선 절차. 번호 매긴 단계로, **어떤 파일/설정을 어떻게 바꾸는지** 구체화. 완료 확인 방법 포함.

#### `escalation_criteria` (string)
임시 조치 실패 판정 기준 + 누구를 호출할지. 예: `"임시 조치 2회 후에도 메트릭 정상화 안 되면 DBA 온콜. SEV-1 트리거 조건: <구체 수치>"`.

#### `prevention_measures` (list[string])
재발 방지용 항목. 각 항목에 "무엇을 어디에 추가/변경" 수준으로 구체화.
예: `"CloudWatch 알람 추가: DatabaseConnections > 임계치 70% 5분 지속 시 경보 (현재 90%)"`.

#### `tags` (list[string])
분류용 키워드. 소문자 kebab-case. 예: `["rds","connection-pool","db-leak"]`.

### 작성 원칙 (플레이북용)

- **복붙 가능한 명령**: 각 절차는 실제 오퍼레이터·에이전트가 **수정 없이** 바로 실행할 수 있는 형태여야 한다. 변동값은 `<placeholder>`로.
- **모호한 동사 금지**: "확인한다", "점검한다" 만 쓰면 안 되고 `get_metric_data(...)`, `aws cloudwatch ...`, `POST /...` 같은 구체 호출을 붙인다.
- **판정 조건 필수**: 모든 단계는 **Pass/Fail 조건**을 가져야 한다. "결과 확인" 같은 open-ended 지시 금지.
- **서술 금지**: 왜 이 장애가 중요한지, 어떤 교훈이 있는지는 보고서(`report.md`)의 역할. 플레이북은 실행 절차만.
