# Vital Sensor

센서 데이터를 PostgreSQL에 저장하고 환자별 바이탈과 이상치 알림을 조회하는
FastAPI 서비스다. RCA 데모는 저장 INSERT 컬럼 하나가 다른 불변 이미지로 재현한다.

- `POST /sensors/data`: 배치 저장과 이상치 판정.
- `GET /patients/{patient_id}/vitals`: 타입·기간 조건으로 기존 데이터 조회.
- `GET /alerts`: 이상치 조회.
- `GET /healthz`: `status`, `db_connected`, `active_db_connections`, `uptime_seconds`.

헬스 확인은 HTTP 200뿐 아니라 `status=ok`, `db_connected=true`를 검사한다.
저장 실패 중에도 DB 연결과 조회가 정상이라면 헬스는 정상이다.

```bash
uv sync --extra dev
uv run uvicorn test_service.main:app --host 0.0.0.0 --port 8000 --no-access-log
uv run ruff check src tests demo
uv run pytest
```

접속 정보는 `DATABASE_URL` 또는 `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USERNAME`,
`DB_PASSWORD`로 제공한다. 배포에서는 자격 증명을 비밀 참조로 주입한다.
기본 풀 크기는 5, 추가 연결 상한은 10이다. 트래픽은 기본 5초 간격, 동시 실행 1개로
제한하며 실행하지 못한 슬롯을 별도 계수한다. `TRAFFIC_ENABLED=false`로 끌 수 있다.

`DB_OBSERVABILITY_ENABLED`는 기본 true이며 false로 끌 수 있다. 기본 5초마다
SQL 지문·연결·실제 스키마를 관측한다. 지표는 요청 완료와 독립적으로 기본 30초마다
발행한다. `PatientVitalsQueryDuration`은 관측 지표로 유지하고 저장 실패 알람만
RCA 토픽으로 전달한다.

불변 소스 빌드, 실제 PostgreSQL 검증과 이벤트 필드는 [데모 안내](demo/README.md)를
참조한다. 브라우저용 설명은 [저장 흐름](demo/mechanisms.html)에 있다.


## 내구 Vital 이벤트

`POST /sensors/events`는 v1 `timestamp` 또는 v2 `sampled_at`을 받는 단일 이벤트 admission이다.
생성자는 서비스 전체에서 현재 DB 초마다 한 이벤트를 내구 저장하고, 별도 bounded worker가
실제 측정 저장/재시도를 수행한다. 202 PENDING은 측정 저장 완료가 아니다. 동일 입력은 재사용하고
다른 내용의 ID는 409로 거부한다. 포화(86,400건)는 신규 수용 503/자동 생성 중단이며 기존 입력은 유지한다.

sensor ID는 별도 보존한다. patient_id 생략 시 고정 합성 데모 subject P-001을 쓰며 실제 환자 연결을
추정하지 않는다. 기존 /sensors/data와 환자·알림 조회는 유지한다. TRAFFIC_INTERVAL_SECONDS는 기존
조회용 scheduler 간격이고 새 이벤트 생성의 1Hz를 변경하지 않는다. TRAFFIC_ENABLED=false는
자동 생성을 끄지만 durable backlog consumer를 취소하지 않는다.

정상·결함·복원은 새 schema/input 경로를 모두 지원하는 같은 소스 쌍이어야 한다. 과거 image의
정상 이력을 새 이벤트의 롤백 호환성 증명으로 사용하지 않는다. 기존 alarm namespace/dimension은 유지한다.

정확한 필드·retention 관계·checkpoint/cohort API·metric 단위는
[이벤트 저장 계약 구현](../../docs/tables/vital-events.md)을 참조한다.
