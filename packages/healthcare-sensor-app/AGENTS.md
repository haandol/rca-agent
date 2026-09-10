# AGENTS.md

> 이 패키지는 RCA Agent 모노레포의 일부입니다. 전체 아키텍처, ADR, 크로스 패키지 계약, 빌드 명령어는 **[루트 AGENTS.md](../../AGENTS.md)** 를 참조하세요.

## Project Overview

Healthcare Sensor App은 RCA 에이전트의 근본원인분석 정확도를 검증하기 위한 헬스케어 센서 데이터 수집/조회 서비스다. 환자 바이탈 사인을 수집하고, 이상치를 자동 감지하며, 의도적 장애 주입(fault injection) 기능으로 다양한 인시던트 시나리오를 재현할 수 있다.

### Core Features

- **센서 데이터 수집**: 심박수, 혈압(수축기/이완기), 체온, SpO2 배치 수집
- **이상치 감지**: 임계값 기반 자동 이상치 판별 및 알림
- **환자별 바이탈 조회**: 타입/기간 필터링 지원
- **실제 결함 데모**: 풀 설정 회귀, 불변 리비전의 조회 증폭·세션 정리 회귀, 별도 정비 트랜잭션의 쓰기 차단을 정상 → 결함 → 복원으로 비교
- **레거시 장애 제어**: 기존 DB 커넥션 릭, CPU 부하, 메모리 압박, 슬로우 쿼리 API와 환경 플래그 유지 (high-cpu, slow-query는 명시적 reset API 호출까지 지속)
- **증상 지표(EMF)**: 구조화 로그에 지표를 담는 Embedded Metric Format으로 저장 완료·실패·시작·진행 중 상태와 환자 조회 시간을 관측. 요청 완료와 독립적으로 주기 방출
- **Bounded Traffic Generator**: 기본 5초마다 개별 요청 시작을 계획하고 동시 실행을 1개로 제한. 저장·환자 조회와 주기적인 알림 조회를 수행하며 실행하지 못한 요청도 계수
- **OpenTelemetry 계측**: 분산 트레이싱 및 메트릭 수집

### Tech Stack

- **Framework**: FastAPI
- **Language**: Python 3.13+
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
불변 이미지 빌드, 정비 작업, 호출자 소유 PostgreSQL을 사용하는 로컬 검증은
[실제 PostgreSQL 데모 안내](./demo/README.md), 실행 인자와 결과물은
[로컬 runner 사용법](./demo/README.md#로컬-실행)을 참조한다.
브라우저에서 읽는 흐름 설명은 [메커니즘 설명](./demo/mechanisms.html)에 있다.

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
│       │   ├── symptom_metrics.py        # bounded sample buffer + 주기적 EMF 발행
│       │   ├── db_observability.py       # 요청별 SQL hash·횟수·시간·연결 획득/반환
│       │   ├── health.py                 # 헬스 체크 (DB 풀 상태 포함)
│       │   ├── fault.py                  # 레거시 장애 제어 API
│       │   └── traffic_generator.py      # 개별 요청 시작 계획·동시 실행 상한·누락 계수
│       ├── revision/                    # 빌드 시 고정하는 조회·세션 정리 구현과 소스 manifest
│       ├── maintenance.py               # 실행 ID·유한 유지 시간을 가진 별도 락 작업 CLI
│       ├── telemetry.py                  # OpenTelemetry 설정
│       └── main.py                       # FastAPI entrypoint
├── demo/
│   ├── README.md                         # 이미지·정비 작업·DB 관측·로컬 runner 안내
│   ├── mechanisms.html                   # 브라우저용 메커니즘 설명
│   ├── build_revision.py                 # 공통 소스 스냅샷·리비전 빌드·해시 기록
│   ├── revisions/                        # r2 조회 증폭 / r3 세션 정리 결함 소스
│   ├── local_runner.py                   # 소유 PostgreSQL에서 정상 → 결함 → 복원 검증
│   └── local_worker.py                   # 구간별 실제 서비스·SQL 실행과 증거 수집
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

### 불변 리비전과 실제 메커니즘 검증

| 리비전 | 빌드에 고정되는 동작 |
|--------|----------------------|
| `r1` | 기본 정상 이미지. 환자 데이터를 한 번에 조회하고 성공·예외·취소에서 세션을 정리 |
| `r2` | ID 목록 조회 후 행별로 다시 조회. 동일 결과에 대한 SQL 호출 수를 증폭 |
| `r3` | 연결 획득 후 본문 예외·취소에서 세션 정리가 누락. 정상 입력은 정상 처리 |

`SOURCE_REVISION`은 Docker **빌드 인자**다. 실행 환경의 같은 이름이나
`DEPLOYED_REVISION`을 바꿔 구현을 전환하지 않는다. 빌드 manifest는 설치된 소스의
해시를 기록하며 실행 시 불일치하면 시작을 거부한다. 빌드하지 않은 로컬 원본은
`r1`, `verified=false`로 표시한다.

풀 설정 회귀는 실제 풀 용량 변경으로, 쓰기 차단은 별도 정비 작업의 실제 트랜잭션
락으로 재현한다. `DB_POOL_SIZE >= 1`, `DB_MAX_OVERFLOW >= 0`만 허용하며
기본값은 각각 5와 10이다. 정상·결함·복원 구간에 같은 입력·부하 설정을 사용하고,
복원은 원래 설정·정상 소스·소유 작업 해제로 확인한다.
레거시 reset API가 `r2`/`r3`의 소스를 되돌리거나 `r3`의 누락된 세션 정리를
복구하지는 않는다.

[로컬 runner](./demo/README.md#로컬-실행)는 호출자가 준비한 전용 PostgreSQL과
새 출력 디렉터리를 사용한다. 공통 소스 스냅샷에서 리비전을 빌드하고 구간별 프로세스로
조회·풀·락·세션 실험을 실행한다. 소스 해시, 실제 SQL·연결 관측, 응답 비교와 정리 결과를
남기며 소유 프로세스·연결·스키마를 정리한다. 로컬 성공과 조회 지연 후보 임계치는
AWS 알람 발화·모델 평가·승인 실행의 검증 결과로 간주하지 않는다.

### 제한된 배경 부하와 증상 관측

저장과 환자 조회를 번갈아 계획하고 여섯 쌍마다 알림 조회 한 슬롯을 넣는다.
`TRAFFIC_INTERVAL_SECONDS`는 **개별 요청 슬롯 간격**이며 요청 완료 후 대기 시간이
아니다. 동시 실행 상한에 도달하거나 스케줄러가 늦어 놓친 슬롯은 실행하지 않는다.
무한 대기열이나 뒤늦은 몰아 보내기 없이 `TrafficSkipped`로 기록한다.
기본 환자 집합은 가상 환자 10명이며 생성 시 비정상 범위 프로필을 8% 확률로 선택한다.

| 설정 | 기본값 |
|------|--------|
| `TRAFFIC_ENABLED` / `TRAFFIC_INTERVAL_SECONDS` / `TRAFFIC_MAX_CONCURRENCY` | `true` / `5` / `1` |
| `TRAFFIC_QUERY_LIMIT` / `TRAFFIC_PATIENT_ID` / `TRAFFIC_SEED` | `20` / 미지정 / 미지정 |
| `METRIC_FLUSH_INTERVAL_SECONDS` | `30` |
| `DB_POOL_TIMEOUT_SECONDS` / `DB_STATEMENT_TIMEOUT_MS` | `30` / `0` |
| `DB_OBSERVABILITY_ENABLED` / `DB_OBSERVABILITY_INTERVAL_SECONDS` | `false` / `5` |

이 값은 구현 조정값이며 실측하지 않은 데모 성공 조건이나 알람 보장값이 아니다.
고정 환자와 seed를 지정하면 비교 구간의 입력을 재현할 수 있다.

지표는 `Healthcare/Sensor`, `ServiceName=healthcare-sensor-app`으로 발행한다.

- `VitalIngestAttempts` / `VitalIngestFailures`: 저장 성공·오류로 종료된 리딩 수와 실패 리딩 수. 취소된 미완료 리딩은 기존 완료 계수에 넣지 않는다.
- `VitalIngestStarted` / `VitalIngestInFlight`: 시작한 리딩 수와 현재 진행 중인 리딩 수. 진행 중 값은 주기 방출 후에도 유지하고 종료·실패·취소에서 해제한다.
- `PatientVitalsQueryDuration`: 서비스 메서드의 환자 조회 시간, 단위 `Milliseconds`. 내부 호출과 성공·오류·취소를 모두 기록하며 조회 알람의 `Average` 입력으로 사용한다.
- `AbnormalAlertDelaySeconds`: 기존 비정상 리딩 저장 지연, 단위 `Seconds`. 환자 조회 시간과 별도 지표다.
- `TrafficOffered` / `TrafficStarted` / `TrafficCompleted` / `TrafficSkipped`: 계획된 요청 슬롯, 실제 시작, 종료, 실행하지 못한 슬롯. `TrafficCompleted`는 오류·취소 종료도 포함하고 `TrafficFailed` / `TrafficCancelled`로 구분한다.

EMF 샘플 배열은 최대 100개이며 가득 차면 방출해 샘플을 버리지 않는다.
독립된 주기 작업은 DB 대기 중에도 진행 중 값과 누락 계수를 발행한다.
DB 관측을 활성화하면 실제 SQLAlchemy 어댑터의 제한된 별도 관측 연결도 사용한다.
서비스의 `operation_context("ingest" | "patient_vitals" | "alerts")`가 요청 ID와
SQL·연결 통계를 연결하며 SQL 원문·바인드 값·자격 증명은 관측 로그에 넣지 않는다.
종료 시 요청과 DB 관측 작업을 취소하고 종료를 기다린 뒤 최종 지표를 방출하고
DB 자원을 정리한다. 세부 인터페이스는 [DB 관측 연동](./demo/README.md#db-관측-연동)을 참조한다.

### 레거시 직접 장애 주입 시나리오

1. Fault injection API로 특정 장애 유형 트리거 (high-cpu, slow-query는 reset 호출까지 영구 지속)
2. CloudWatch/X-Ray에서 이상 징후 포착
3. RCA 에이전트가 알람을 수신하고 근본원인분석 수행
4. (Remediation 활성화 시) 에이전트가 reset API를 자동 호출하여 장애 해제
5. 분석 결과를 기대 원인과 비교하여 정확도 측정

### 레거시 플래그 기반 배포 장애 주입

`scripts/inject_deployment_fault.py`가 ECS 태스크 정의 리비전을 등록하고 서비스를
갱신한다. CloudTrail에 실제 `RegisterTaskDefinition`·`UpdateService` 이벤트가 남아
장애 시작 시각과 배포 시각이 조작 없이 일치한다.

1. `inject_deployment_fault.py db-leak` — `FAULT_DB_LEAK=true` 리비전 배포
2. 환자 바이탈 조회 요청마다 세션이 반환되지 않아 커넥션이 점진적으로 누적
3. 바이탈 저장이 실패하기 시작하고 `VitalIngestFailures` 증상 알람 발생
4. 에이전트가 증상 → 커넥션 추세 → 배포 이벤트 → 코드 경로 순으로 원인 특정
5. `inject_deployment_fault.py reset` — 모든 장애 플래그를 끈 리비전 배포

리셋 API는 지원하는 실행 중 장애 상태를 해소한다. 환경 플래그와 불변 소스까지
복원한다고 가정하지 않는다. 배포 설정을 되돌리는 조치는 별도로 수행해야 한다.

`inject_deployment_fault.py red-herring`은 `LOG_LEVEL`만 바꾼 무해한 배포를
만든다. 두 배포 중 어느 것이 증상 시작 시각과 상관되는지 구별해야 원인에 도달한다.

### 서비스 디스커버리

ECS 배포 시 Cloud Map Private DNS로 등록된다: `healthcare.rcaagentdev.local:8000`.
레거시 Remediation 경로는 이 DNS로 reset API를 호출한다. 새 시나리오의 복원은
해당 결함의 원래 설정·이미지 복원 또는 소유 정비 작업 해제를 따른다.

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
