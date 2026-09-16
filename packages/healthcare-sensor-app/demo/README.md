# 단일 INSERT 컬럼 데모

같은 소스 스냅샷에서 `v1`과 `v2`를 만든다. 두 소스의 차이는
`revision/write.py`의 `TIMESTAMP_COLUMN` 상수 한 곳이다. `v1`은 실제
`timestamp`에 저장하며 `v2`는 없는 `sampled_at`을 참조한다. 두 이미지 모두
동일한 ORM 모델, 테이블 초기화, 조회, 헬스, 트랜잭션 정리를 사용한다.
실패는 PostgreSQL이 실제 INSERT를 실행할 때 발생하는 SQLSTATE `42703`이다.

## 빌드

`SOURCE_REVISION`은 Docker 빌드 인자다. 런타임 환경 변수로 구현을 전환할 수 없다.
기본값은 `v1`이며 다른 과거 revision은 받지 않는다. 실제 데모 이미지를 함께
만들 때는 작업공간을 한 번 캡처하고 그 스냅샷을 양쪽 빌드 입력으로 사용한다.
`build_revision.capture_source_snapshot`과 `compile_revision(source_package=...)`가
그 경로를 제공한다. 설치 소스가 manifest와 다르면 기동을 거부한다.
빌드하지 않은 개발 소스는 `verified=false`여서 정상 기준 증거로 사용할 수 없다.

## 로컬 실행

호출자가 소유한 PostgreSQL 17을 `127.0.0.1:15439`, DB `rca_demo`로 준비한다.
runner는 컨테이너를 만들거나 기존 5432 서버를 건드리지 않는다. 자격 증명은
접근 제한된 env 파일에 두고 출력물에는 포함하지 않는다.

```bash
uv run python demo/local_runner.py \
  --env-file /private/tmp/healthcare-v1v2-database.env \
  --expected-port 15439 \
  --output /private/tmp/healthcare-proof-new-run
```

출력 디렉터리는 새 경로여야 한다. 다른 포트를 명시할 수 있으나 loopback,
DB 이름, 포트 일치 조건을 검사하고 5432는 거부한다. runner는 실행별 스키마와
불변 소스 트리를 만들고 `v1 → v2 → v1`의 별도 프로세스로 검증한다.
각 구간은 같은 2행 요청을 3회 실행한다. 정상·복구는 각 6행 저장, 실패 구간은
추가 0행과 실제 `42703`을 요구한다. 기존 데이터, 알림 조회, 헬스 본문,
커넥션 반환, 시작 시 스키마 관측과 로그·HTTP 오류·트레이스의 canary 비노출을 검사한다.
스키마 전제가 다르면 컬럼을 추가해 맞추지 않고 중단한다.

`evidence.json`은 소스 지문, 구간별 원본 이벤트, 완료·실패 계수와 정리 결과를 담는다.
종료·실패·취소에서 자신이 만든 프로세스 그룹과 스키마만 정리한다. 컨테이너는
호출자 소유이므로 유지한다. 로컬 통과는 AWS 알람·RCA·승인 실행의 통과가 아니다.

## 이벤트 wire

모든 이벤트의 `observed_at`은 UTC ISO 8601이다. CloudWatch의 원본 timestamp,
event ID, log group, log stream은 수집자가 원본 응답에서 보존한다.

| event | 필드와 의미 |
|---|---|
| `source_manifest` | `revision`, `verified`, `fingerprint`, `base_fingerprint`, `files`. 빌드 소스는 전부 검증되며 개발 원본에는 base 지문이 없다. |
| `write_contract` | `operation`, `request_id`, `sql_hash`, `sql_hash_algorithm=sha256`, `schema_name`, `table_name`, `column_names`. 실제 드라이버 바인딩 SQL 형태의 지문이며 SQL 원문과 값은 없다. 기동 이벤트의 request ID는 null이다. |
| `db_write_error` | write 계약 필드와 실제 드라이버의 `sqlstate`, `error_type`, `schema_name`, `driver_table_name`, `driver_column_name`. 없는 필드는 null이며 메시지에서 추정하지 않는다. |
| `db_schema_snapshot` | 별도 읽기 전용 트랜잭션에서 조회한 `schema_name`, `table_name`, `column_names`. search path로 해석되는 실제 INSERT 테이블을 관측한다. |
| `write_completed` | write 계약 필드와 `count`, `completion_semantics=committed_rows`. 커밋 성공 이후에만 발행한다. |
| `write_accounting` | 아래 표의 생산자 계수 정책. |
| `ecs_runtime_identity` | 기존 실제 ECS 메타데이터 응답 기반 태스크·컨테이너 식별. 로컬에서는 미확인 상태다. |

`write_contract`와 `write_completed`의 `schema_name`은 null이다. INSERT는 schema를
명시하지 않으므로 실제 관계는 `db_schema_snapshot`과 연결해 판단한다. 오류의
컬럼 진단이 null이면 `column_names`와 실제 스키마를 비교한다.

| write_accounting 필드 | 값 |
|---|---|
| `metric_namespace` / `service_name` | `Healthcare/Sensor` / `healthcare-sensor-app` |
| `attempt_metric` / `failure_metric` | `VitalIngestAttempts` / `VitalIngestFailures` |
| `attempt_semantics` | `completed_successful_rows_plus_failed_rows` |
| `failure_semantics` | `failed_rows` |
| `cancellation_semantics` | `excluded_from_completed_counters` |
| `success_evidence_event` / `success_count_field` | `write_completed` / `count` |
| `success_semantics` | `committed_rows` |

시작 계수는 `VitalIngestStarted`, 진행 중 계수는 `VitalIngestInFlight`다.
완료 계수와 구분하며 취소는 완료 계수에서 제외한다. 완료 이벤트를 포함한 원본
관측은 서비스·태스크·시간 범위가 일치하는 경우에만 정상 기준으로 결합한다.

## 검증 명령

```bash
uv run ruff check src tests demo
HEALTHCARE_PROOF_ENV_FILE=/private/tmp/healthcare-v1v2-database.env uv run pytest
```

HTTP 트레이싱의 request hook과 헤더 값 마스킹은 OpenTelemetry Python Contrib의
FastAPI/ASGI 문서를 Context7로 확인했다. 테스트는 실제 설치 버전에서 span 속성,
이벤트, 로그와 오류 응답 전체에 canary가 없는지 확인한다.
