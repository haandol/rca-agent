# AGENTS.md

> 이 패키지는 RCA Agent 모노레포의 일부입니다. 전체 아키텍처, ADR, 크로스 패키지 계약, 빌드 명령어는 **[루트 AGENTS.md](../../AGENTS.md)** 를 참조하세요.

## Project Overview

Healthcare Sensor App은 RCA 에이전트의 근본원인분석 정확도를 검증하기 위한 헬스케어 센서 데이터 수집/조회 서비스다. 환자 바이탈 사인을 수집하고, 이상치를 자동 감지하며, 의도적 장애 주입(fault injection) 기능으로 다양한 인시던트 시나리오를 재현할 수 있다.

### Core Features

- **센서 데이터 수집**: 심박수, 혈압(수축기/이완기), 체온, SpO2 배치 수집
- **이상치 감지**: 임계값 기반 자동 이상치 판별 및 알림
- **환자별 바이탈 조회**: 타입/기간 필터링 지원
- **장애 주입**: DB 커넥션 릭, CPU 부하, 메모리 압박, 슬로우 쿼리 (high-cpu, slow-query는 명시적 reset API 호출까지 영구 지속)
- **Background Traffic Generator**: 10명 가상 환자에 대해 5초 간격 센서 데이터 자동 생성 (92% 정상, 8% 비정상) — CloudWatch baseline 메트릭 축적용
- **OpenTelemetry 계측**: 분산 트레이싱 및 메트릭 수집

### Tech Stack

- **Framework**: FastAPI
- **Language**: Python 3.12+
- **Package Manager**: uv
- **Architecture**: Hexagonal (Ports and Adapters)
- **ORM**: SQLAlchemy 2.0 (async)
- **Database**: PostgreSQL (asyncpg)
- **Observability**: OpenTelemetry (OTLP gRPC)
- **Lint/Format**: ruff
- **Test**: pytest + pytest-asyncio + httpx

## Quick Start

```bash
# 로컬 인프라 실행
docker compose up -d

# 의존성 설치
uv sync

# 개발 서버
uv run uvicorn test_service.main:app --reload --host 0.0.0.0 --port 8000

# 린트 & 테스트
uv run ruff check src/ tests/
uv run pytest
```

자세한 설정은 [README.md](./README.md)를 참조.

## Project Structure

```
packages/healthcare-sensor-app/
├── src/
│   └── test_service/
│       ├── adapters/
│       │   ├── primary/                   # FastAPI controllers
│       │   │   ├── sensors/               # POST /sensors/data
│       │   │   ├── patients/              # GET /patients/{id}/vitals
│       │   │   ├── alerts/                # GET /alerts
│       │   │   ├── health/                # GET /healthz
│       │   │   ├── fault/                 # POST /fault/*
│       │   │   └── schemas.py             # Pydantic request/response schemas
│       │   └── secondary/                 # Infrastructure adapters
│       │       ├── database_adapter.py    # SQLAlchemy async engine
│       │       └── sensor_repository/     # Sensor reading CRUD
│       │           ├── models.py          # SQLAlchemy ORM models
│       │           └── sqlalchemy_sensor_repository.py
│       ├── config/
│       │   └── settings.py               # AppSettings (frozen dataclass + lru_cache)
│       ├── di/
│       │   ├── container.py              # Container ABC
│       │   └── app_container.py          # Lazy @property DI wiring
│       ├── middleware/
│       │   └── logging.py                # Request logging middleware
│       ├── ports/
│       │   ├── dto/
│       │   │   └── sensor.py             # SensorReadingEntity, ReadingType
│       │   └── interfaces/
│       │       ├── database.py           # DatabasePort ABC
│       │       └── sensor_reading_repository.py  # SensorReadingRepositoryPort ABC
│       ├── services/
│       │   ├── sensor.py                 # 바이탈 수집, 이상치 판별, 조회
│       │   ├── health.py                 # 헬스 체크 (DB 풀 상태 포함)
│       │   ├── fault.py                  # 장애 주입 (커넥션 릭, CPU, 메모리, 슬로우 쿼리)
│       │   └── traffic_generator.py      # Background 센서 데이터 자동 생성 (lifespan task)
│       ├── telemetry.py                  # OpenTelemetry 설정
│       └── main.py                       # FastAPI entrypoint
├── tests/                                # pytest tests
├── docker-compose.yml                    # PostgreSQL 16 + DynamoDB Local + ADOT Collector
├── otel-collector-config.yaml            # ADOT Collector 로컬 설정
├── pyproject.toml                        # uv/ruff/pytest 설정
└── package.json                          # Nx scripts (lint, format, test, dev)
```

## Key Workflows

### 센서 데이터 수집

1. 클라이언트가 센서 리딩 배치를 POST
2. `SensorService`가 각 리딩의 이상치 여부를 임계값 기반으로 판별
3. `SensorReadingRepository`가 PostgreSQL에 배치 저장
4. 응답으로 저장된 리딩 목록 반환

### 장애 주입 시나리오

1. Fault injection API로 특정 장애 유형 트리거 (high-cpu, slow-query는 reset 호출까지 영구 지속)
2. CloudWatch/X-Ray에서 이상 징후 포착
3. RCA 에이전트가 알람을 수신하고 근본원인분석 수행
4. (Remediation 활성화 시) 에이전트가 reset API를 자동 호출하여 장애 해제
5. 분석 결과를 기대 원인과 비교하여 정확도 측정

### 서비스 디스커버리

ECS 배포 시 Cloud Map Private DNS로 등록된다: `healthcare.rcaagentdev.local:8000`. RCA 에이전트의 Remediation 단계에서 이 DNS로 reset API를 호출한다.

## Architecture Principles

### Core Patterns

- **Hexagonal Architecture**: 도메인과 인프라의 명확한 분리
- **Dependency Inversion**: 서비스는 Port 인터페이스에만 의존
- **Lazy Initialization**: DI 컨테이너의 `@property`로 온디맨드 생성
- **Resource Cleanup**: lifespan context manager에서 정리

### DI Container Pattern

```python
# Container ABC
class Container(ABC):
    @property
    @abstractmethod
    def settings(self) -> AppSettings: ...
    @abstractmethod
    def create_router(self) -> APIRouter: ...
    @abstractmethod
    async def cleanup(self) -> None: ...

# AppContainer — lazy @property 기반 wiring
class AppContainer(Container):
    @property
    def database(self) -> DatabasePort:
        if self._database is None:
            self._database = SqlAlchemyDatabaseAdapter(self.settings)
        return self._database
```

## Agent Guidelines

### Safe to Modify

- Service 파일 (`services/`)
- Secondary adapter (`adapters/secondary/`)
- Primary controller (`adapters/primary/`)
- DTO (`ports/dto/`)

### Approach with Caution

- `main.py` — 앱 엔트리포인트
- `di/app_container.py` — DI 배선
- `config/settings.py` — 환경 설정
- Port 인터페이스 (`ports/interfaces/`)
- Middleware (`middleware/`)

### Common Mistakes to Avoid

- Direct adapter 인스턴스화 (DI 컨테이너 사용)
- 서비스가 구현체에 직접 의존
- 타입 힌트 누락
- async 함수에서 blocking I/O 사용
- `source .venv/bin/activate` 사용 (`uv run` 사용)
