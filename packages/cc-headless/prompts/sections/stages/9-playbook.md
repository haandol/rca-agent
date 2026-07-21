## 9단계: 플레이북 생성 (직접 수행)

플레이북은 **유사 장애 재발 시 별도 Remediation Agent 또는 승인된 오퍼레이터가
실행할 수 있는 절차서**이다. CC Headless는 절차를 작성할 뿐 서비스·인프라 변경을
실행하지 않는다. 보고서와 달리 서술 설명·교훈·맥락은 배제하고, **호출 후보,
사전조건, 승인, 롤백 조건, 기대 출력, 판정 조건**을 담는다.

1. RCA 보고서에서 장애 유형, 증상 패턴, 증거 수집 경로와 복구 권고를 추출한다.
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
**각 항목은 별도 실행 주체가 독립 수행할 수 있는 마크다운 블록**. 템플릿:

```
### {N}. {단계 제목}
- **검증 메트릭**: {namespace/metric/dimensions 또는 로그·상태}
- **조회 후보**: {MCP 도구명과 쿼리 파라미터}
- **관측 조건**: {변경 성공 확인 후, period, 관측 구간, 데이터포인트 수}
- **Query**:
  ```
  {별도 실행 주체가 사용할 쿼리·파라미터}
  ```
- **기준값**: {장애 전 baseline과 알람 임계치}
- **Pass 판정**: {정상화 정량 조건}
- **Fail 판정**: {롤백 또는 에스컬레이션 정량 조건과 담당 주체}
```

최소 3단계 권장: (1) 알람 메트릭 재확인 (2) 상관 메트릭·로그 수집 (3) 근본원인 특정.

#### `temporary_mitigation` (string)
**복구 권고 후보**를 번호 매긴 단계로 작성한다. 각 후보에 다음을 반드시 포함한다:

- **제안할 액션**: 별도 실행 주체가 사용할 HTTP 또는 ECS 액션 후보
- **사전조건**: 신뢰도, 대상 리소스, 현재 상태, 가용성·백업 조건
- **승인 필요**: 승인 필요 여부와 승인 주체
- **기대 결과**: 응답 또는 상태 전이
- **롤백 조건**: 오류율, 메트릭 악화, 안정화 실패의 정량 기준
- **실행 상태**: `CC Headless 미실행`

Healthcare Service 엔드포인트 매핑:
- 커넥션 누수 → 후보 `POST /fault/db-leak/reset`
- 높은 CPU → 후보 `POST /fault/high-cpu/reset`
- 메모리 부족 → 후보 `POST /fault/high-memory/reset`
- 느린 쿼리 → 후보 `POST /fault/slow-query/reset`
- 매칭 없음 → 후보 `UpdateService(forceNewDeployment=true)`

#### `permanent_remediation` (string)
영구 개선 권고. 번호 매긴 단계로, **어떤 파일/설정을 어떻게 바꿀지**와 변경 전
승인·테스트·롤백 조건을 구체화한다.

#### `escalation_criteria` (string)
임시 조치 실패 판정 기준 + 누구를 호출할지. 예: `"임시 조치 2회 후에도 메트릭 정상화 안 되면 DBA 온콜. SEV-1 트리거 조건: <구체 수치>"`.

#### `prevention_measures` (list[string])
재발 방지용 항목. 각 항목에 "무엇을 어디에 추가/변경" 수준으로 구체화.
예: `"CloudWatch 알람 추가: DatabaseConnections > 임계치 70% 5분 지속 시 경보 (현재 90%)"`.

#### `tags` (list[string])
분류용 키워드. 소문자 kebab-case. 예: `["rds","connection-pool","db-leak"]`.

### 작성 원칙 (플레이북용)

- **CC 실행 금지**: 모든 변경 액션은 후보로만 기록한다. 호출 결과나 정상화 결과를 만들어내지 않는다.
- **구체적인 후보**: 별도 실행 주체가 검토할 수 있도록 변동값은 `<placeholder>`로 두고 액션·쿼리와 파라미터를 명시한다.
- **모호한 동사 금지**: "확인한다", "점검한다" 만 쓰지 말고 구체적인 조회 후보와 정량 판정 기준을 붙인다.
- **판정 조건 필수**: 모든 단계는 **Pass/Fail 조건**을 가져야 한다. "결과 확인" 같은 open-ended 지시 금지.
- **서술 금지**: 왜 이 장애가 중요한지, 어떤 교훈이 있는지는 보고서(`report.md`)의 역할. 플레이북은 실행 절차만.
