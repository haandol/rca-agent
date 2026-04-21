# Healthcare Sensor App

RCA 에이전트 검증을 위한 헬스케어 센서 데이터 수집/조회 서비스. 환자 바이탈 사인(심박수, 혈압, 체온, SpO2)을 수집하고, 이상치 감지 및 알림을 제공한다. 의도적 장애 주입(fault injection) 기능으로 RCA 에이전트의 근본원인분석 정확도를 검증할 수 있다.

## Project Layout

```
src/
|- test_service/
   |- adapters/
   |  |- primary/             # FastAPI controllers (HTTP endpoints)
   |  |  |- sensors/          # 센서 데이터 수집 API
   |  |  |- patients/         # 환자별 바이탈 조회 API
   |  |  |- alerts/           # 이상치 알림 API
   |  |  |- health/           # Health check
   |  |  |- fault/            # 장애 주입 API
   |  |- secondary/           # External service adapters
   |  |  |- database_adapter.py  # SQLAlchemy async engine (PostgreSQL)
   |  |  |- sensor_repository/   # Sensor reading persistence
   |- config/                 # Environment-backed settings
   |- di/                     # Dependency injection container
   |- middleware/              # Logging middleware
   |- ports/                  # DTOs and abstract port contracts
   |  |- dto/                 # Data transfer objects
   |  |- interfaces/          # Port interfaces (ABC)
   |- services/               # Application services
   |- telemetry.py            # OpenTelemetry setup
   |- main.py                 # FastAPI entrypoint
tests/                        # pytest tests
docker-compose.yml            # PostgreSQL + DynamoDB Local + ADOT Collector
otel-collector-config.yaml    # ADOT Collector 로컬 설정 (debug exporter)
```

## Getting Started

1. 로컬 인프라 실행:

   ```bash
   docker compose up -d
   ```

2. 의존성 설치 ([uv](https://docs.astral.sh/uv/)):

   ```bash
   uv sync
   ```

3. 개발 서버 시작:

   ```bash
   uv run uvicorn test_service.main:app --reload --host 0.0.0.0 --port 8000
   ```

4. 린트 & 포맷:

   ```bash
   uv run ruff check src/ tests/
   uv run ruff format src/ tests/
   ```

5. 테스트:

   ```bash
   uv run pytest
   ```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL 연결 문자열 (asyncpg) | `postgresql+asyncpg://postgres:postgres@localhost:5432/test_service` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OpenTelemetry OTLP 엔드포인트 | `http://localhost:4317` |
| `OTEL_SERVICE_NAME` | OpenTelemetry 서비스 이름 | `healthcare-sensor-app` |
| `LOG_LEVEL` | 로그 레벨 | `INFO` |
| `FAULT_INJECTION_ENABLED` | 장애 주입 API 활성화 여부 | `true` |
| `DB_POOL_SIZE` | DB 커넥션 풀 크기 | `5` |
| `DB_MAX_OVERFLOW` | DB 커넥션 풀 오버플로우 | `10` |

## API Surface

### Sensor Data

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sensors/data` | POST | 센서 리딩 배치 수집 |

### Patient Vitals

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/patients/{patient_id}/vitals` | GET | 환자별 바이탈 사인 조회 (타입/기간 필터) |

### Alerts

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/alerts` | GET | 이상치 알림 목록 조회 (환자/타입/기간 필터) |

### Health Check

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/healthz` | GET | Liveness probe |

### Fault Injection

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/fault/db-leak` | POST | DB 커넥션 릭 유발 |
| `/fault/db-leak/reset` | POST | 릭된 커넥션 해제 |
| `/fault/high-cpu` | POST | CPU 부하 생성 |
| `/fault/high-memory` | POST | 메모리 할당 |
| `/fault/high-memory/reset` | POST | 할당된 메모리 해제 |
| `/fault/slow-query` | POST | 지연 쿼리 실행 |

Request/response 스키마는 `src/test_service/ports/dto/`와 `src/test_service/adapters/primary/schemas.py`에 정의되어 있다.

## Implementation Notes

- `SensorService`는 바이탈 사인 수집, 이상치 판별, 환자별 조회를 담당한다. 이상치 임계값: HR 60-100, BP systolic 90-140, BP diastolic 60-90, 체온 36-38, SpO2 95-100.
- `FaultInjectionService`는 DB 커넥션 릭, CPU 부하, 메모리 압박, 슬로우 쿼리를 의도적으로 발생시킨다. `FAULT_INJECTION_ENABLED=false`로 비활성화 가능.
- `HealthService`는 DB 커넥션 풀 상태(체크아웃 수, 풀 크기)를 포함한 헬스 체크를 제공한다.
- DI 컨테이너는 lazy `@property` 패턴으로 서비스를 초기화하며, `cleanup()`에서 DB 엔진을 정리한다.
- OpenTelemetry 계측이 FastAPI에 자동 적용되어 분산 트레이싱을 지원한다.
