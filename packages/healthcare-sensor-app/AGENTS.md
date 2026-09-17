# Healthcare Sensor App 작업 지침

이 패키지는 FastAPI, SQLAlchemy async, asyncpg, PostgreSQL을 사용하는 센서 서비스다.
공통 규칙은 루트 AGENTS.md를 적용한다. 현재 결정은 infra/0004와 infra/0007이다.

## 개발 명령과 코드 구조

패키지 디렉토리에서 실행한다. Python 모듈명은 디렉토리명과 다른 `test_service`다.

```bash
uv run uvicorn test_service.main:app --reload --host 0.0.0.0 --port 8000 --no-access-log
uv run ruff format src tests demo
docker compose up -d postgres
```

Compose의 PostgreSQL은 일반 개발용 PostgreSQL 16/5432 설정이다. 아래 실제 DB 증거
검증에 사용하는 PostgreSQL 17/15439와 구분한다.

- FastAPI controller는 `self.router`를 보유하는 클래스 패턴을 사용한다.
- DI는 `Container` ABC를 구현한 `AppContainer`의 lazy `@property` 패턴을 따른다.
- Port 인터페이스는 `ports/interfaces/`의 ABC로 정의한다.
- 도메인 DTO는 `ports/dto/`의 dataclass로 정의한다.

## 구현 경계

서비스는 Port에 의존하고 DI 컨테이너가 어댑터를 연결한다. ORM 모델과 테이블 정의는
`adapters/secondary/sensor_repository/models.py`, 저장 전용 SQL은 `revision/write.py`에 있다.
정상·변경 소스는 같은 캡처에서 만들고 `TIMESTAMP_COLUMN` 상수만 다르게 한다.
revision ID는 `v1`, `v2`이며 런타임 플래그로 구현을 바꾸지 않는다.
결함 파일은 `demo/revisions/v2/revision/write.py`에 보존하고 빌드에서 그대로 복사한다.
빌드 전 정상 파일과 상수 한 곳 외에는 byte 차이가 없는지 검사하며 캡처에 두 파일을 포함한다.
선택적 `source_locations`는 설치 파일 해시와 연결된 Git 조회 위치의 선언일 뿐이다.
로그에는 소스 본문을 넣지 않으며 실제 GitHub 불변 원문 읽기·해시 검증 없이 Git 소스로 인정하지 않는다.

조회, 헬스, 세션 정리는 공통 경로다. session context는 성공 시 커밋하고 실패·취소에서도
닫는다. 완료 로그는 커밋 뒤에만 기록한다. 실제 오류의 허용된 필드만 기록하며 SQL 원문,
매개변수, 환자 값, 자격 증명을 로그·오류·트레이스에 넣지 않는다.

`main.py`의 lifespan이 트래픽·관측·지표 작업을 시작하고 종료를 기다린 뒤 연결을 정리한다.
DB 관측은 기본 true, 트래픽은 기본 5초 간격과 동시 실행 1개다. 서비스 기능과 계수 의미는
[README](README.md), 데모 wire·실행 방법은 [demo/README](demo/README.md)를 참조한다.

## 검증

```bash
uv run ruff check src tests demo
uv run pytest
```

실제 DB 증거는 `demo/local_runner.py`로 별도 검증한다. 호출자가 소유한 loopback의
PostgreSQL 17, DB `rca_demo`를 사용한다. 기본 검증 포트는 15439이며 5432는 거부한다.
다른 DB·컨테이너를 정리하지 않는다. 테스트의 스키마 override는 proof 프로세스에만
적용하며 서비스 모델·스키마를 결함에 맞춰 변경하지 않는다.

일반 조회·헬스·계수·HTTP 오류 차단과 취소/프로세스 정리의 부정 테스트를 보존한다.
함수 docstring에는 동작 이유와 계약을 적는다. `uv run`을 사용하고 가상환경을 source하지 않는다.
