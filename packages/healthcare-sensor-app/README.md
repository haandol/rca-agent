# Healthcare Sensor App

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
