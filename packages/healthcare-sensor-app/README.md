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
| `FAULT_DB_LEAK` | DB 커넥션 리크 feature flag | `false` |
| `FAULT_SLOW_QUERY_MS` | 요청당 인위적 지연 (ms) | `0` |
| `FAULT_ERROR_RATE` | 요청 실패율 (0.0~1.0) | `0.0` |

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

## Feature Flag 기반 장애 시나리오

환경변수를 변경한 배포만으로 장애를 발생시키고, 롤백(환경변수 원복)으로 복구하는 데모 시나리오.

### 시나리오 1: DB 커넥션 리크 (PRD 데모)

```bash
# 장애 배포: FAULT_DB_LEAK=true 로 환경변수 변경 후 재배포
FAULT_DB_LEAK=true docker compose up -d app

# 결과: 모든 DB 요청에서 커넥션을 반환하지 않아 풀이 고갈됨
# CloudWatch 메트릭: DatabaseConnections 지속 상승 → Alarm 트리거
# 로그: "DB connection not returned (FAULT_DB_LEAK enabled)"

# 복구: 환경변수 원복 후 재배포
FAULT_DB_LEAK=false docker compose up -d app
```

### 시나리오 2: 지연 증가

```bash
# 장애 배포: 모든 비즈니스 요청에 3초 지연 주입
FAULT_SLOW_QUERY_MS=3000 docker compose up -d app

# 결과: p99 latency 급증 → Latency Alarm 트리거
# 로그: "Injecting latency via FAULT_SLOW_QUERY_MS"
```

### 시나리오 3: 간헐적 500 에러

```bash
# 장애 배포: 30% 요청이 500 에러 반환
FAULT_ERROR_RATE=0.3 docker compose up -d app

# 결과: 5xx 에러율 급증 → Error Rate Alarm 트리거
# 로그: "Request rejected by FAULT_ERROR_RATE"
```

### 시나리오 복합

여러 flag를 동시에 설정하여 복합 장애 시나리오도 가능하다:

```bash
FAULT_DB_LEAK=true FAULT_SLOW_QUERY_MS=1000 docker compose up -d app
```
