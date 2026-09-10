# 실제 PostgreSQL 데모

이 디렉터리는 실제 서비스 SQL·연결 수명·트랜잭션을 정상 → 결함 → 복원 순서로
검증한다. 로컬 성공은 AWS 알람 전달, 모델 평가, 승인 실행의 성공을 뜻하지 않는다.
브라우저 설명: [mechanisms.html](mechanisms.html).

## 이미지와 소스 계약

패키지 루트에서 실행한다.

```sh
docker build -t healthcare:r1 .
docker build --build-arg SOURCE_REVISION=r2 -t healthcare:r2 .
docker build --build-arg SOURCE_REVISION=r3 -t healthcare:r3 .
```

| 리비전 | 빌드에 고정되는 코드 |
|---|---|
| `r1` | 한 번에 행 조회, 성공·예외·취소 모두 세션 정리. 기본 이미지. |
| `r2` | ID 목록 SELECT 후 각 ID마다 SELECT. 필터·정렬·행 수·응답 값은 유지한다. |
| `r3` | 연결을 획득한 뒤 본문 예외/취소가 나면 정리 코드에 도달하지 않는다. 정상 입력은 정상 저장된다. |

`demo/build_revision.py`가 `src/test_service/revision/`에 선택한 소스를 설치한다.
다른 리비전의 fixture는 최종 이미지에 포함하지 않는다. 일반 실행 환경의
`SOURCE_REVISION`/`DEPLOYED_REVISION` 값으로 구현을 바꾸지 않는다.
빌드 산출물 `revision/_build_manifest.py`에는 리비전, 전체 Python 소스 파일별
SHA-256 해시와 종합 fingerprint가 들어 있다. 이 파일은 JSON 데이터를 담으며
Python 패키지에 포함되도록 `.py` 확장자를 사용한다. 실행 시 소스 해시가 다르면
시작을 거부하고, 정상 시작 시 `source_manifest` 구조화 로그를 남긴다.
로컬 원본 트리는 `r1`, `verified=false`로 표시한다.

`FAULT_DB_LEAK` 등 레거시 플래그와 reset API는 별도로 유지한다.
reset API는 `r3`의 소스를 바꾸거나 그 소스가 남긴 세션을 복원하지 않는다.
새 이미지 경로의 복원은 `r1` 재배포와 이전 프로세스의 세션 정리다.

## 예외와 취소 입력

현재 환자 조회 API는 `limit`의 상한만 검증한다. 다음 입력은 실제 PostgreSQL의
음수 LIMIT 오류를 발생시킨다.

```text
GET /patients/{synthetic-patient-id}/vitals?limit=-1
```

동일 입력을 `r1`과 `r3`에서 반복한다. `r1`은 요청마다 연결을 반환한다.
`r3`은 획득된 세션을 남기므로 풀 용량에 도달하면 이후 정상 저장도 풀 timeout으로
실패한다. 연결 획득 자체가 실패하면 빈 세션은 닫아서 무한히 보관하지 않는다.
`dispose()`는 이 어댑터가 소유한 세션만 닫으며 다른 어댑터의 세션을 닫지 않는다.

취소 probe는 실제 세션에서 `SELECT pg_backend_pid()`를 완료한 직후 소비자
코루틴을 취소한다. Event는 이 취소 시점을 맞추는 용도이고 DB 대기를 흉내 내지 않는다.
드라이버가 SQL 실행 도중 취소되면 연결을 무효화할 수 있으므로, 이 probe는
**연결 획득 후 소비자 취소** 경로를 명시적으로 검사한다.

새 DB 소비자는 다음처럼 사용한다. 본문 예외가 발생했을 때 `async for` 생성기의
`yield` 안으로 예외가 자동 전달된다고 가정하면 안 된다.

```python
async with database.session_context() as session:
    await session.execute(statement)
```

`session()`/`leaky_session()` 생성기는 기존 소비자 호환용으로 남긴다.
DatabasePort의 `session_context()` 기본 구현은 기존 어댑터의 생성기에도 예외와 종료를
명시적으로 전달한다. SQLAlchemy 어댑터는 빌드된 정리 구현을 직접 사용한다.

## 별도 정비 작업 CLI

서비스와 같은 이미지 및 DB 환경설정/비밀 참조를 사용한다. 별도 ECS task의
command를 아래 인자로 바꿀 수 있으며 새로운 클라우드 서비스는 필요하지 않다.

```sh
python -m test_service.maintenance --run-id run_20260910 --hold-seconds 120
```

- `--run-id`: 1~48자의 영문·숫자·밑줄·하이픈.
- `--hold-seconds`: 0보다 크고 7,200 이하. 기본 120초. 분석·승인까지 기다려야 하는
  라이브 데모는 필요한 실행 예산에 맞춰 유한한 유지 시간을 명시한다.
- `--schema`: 기본 `public`. 로컬 검증은 실행 전용 스키마를 지정한다.
- DB 연결은 기존 `DATABASE_URL` 또는 `DB_*` 설정을 사용한다.
- 자기 연결에서 트랜잭션을 시작하고 `sensor_readings`에 실제 `SHARE` 테이블 락을
  획득한다. 이 락은 INSERT의 `ROW EXCLUSIVE` 락과 충돌한다.
- stdout에 `maintenance_lock_acquired` JSON을 즉시 출력한다:
  `run_id`, `backend_pid`, `hold_seconds`, `transaction_start`, `locks`.
- 락 획득 대기는 최대 10초이며, 연결 시도는 최대 5초다.
- 제한 시간·SIGTERM·SIGINT·예외에서 자기 트랜잭션을 롤백하고 연결을 닫는다.
  성공적으로 정리하면 `maintenance_released`를 출력한다.
- 획득 이벤트에 `acquired_at`, `expires_at`, `max_hold_seconds`가 포함된다.
  해제 이벤트의 `release_reason`은 `hold_expired`, `sigterm`, `sigint`,
  `stop_requested`, `cancelled`, `error`를 구분하고 `released_at`,
  `rollback_complete`도 기록한다. 배포 CLI는 작업이 이미 만료/종료된 경우를
  복원 명령이 유발한 해제로 기록하면 안 된다. task 상태와 해제 이벤트를 확인하고,
  자기 명령 전부터 락이 없었던 경우 별도로 보고해야 한다.
- 다른 backend를 종료하는 SQL이나 다른 실행의 task 종료 명령은 실행하지 않는다.

## DB 관측 연동

```python
from test_service.services.db_observability import operation_context

with operation_context("patient_vitals"):
    rows = await repository.find_by_patient(patient_id)

await database.observe(stop_event, interval=5)
```

`operation_context`는 동기 context manager이고 반환값을 요구하지 않는다.
중첩 호출은 같은 요청 ID와 카운터를 공유한다. SQLAlchemy hooks는 SQL hash,
횟수·시간, 획득·반환, 획득 소요 시간을 요청 ID와 연결한다. SQL 원문도
inline literal을 담을 수 있으므로 **원문·매개변수·연결 URL·드라이버 오류 문자열을
관측 로그에 넣지 않는다**. `db_pool_config`가 유효 설정을 기록한다.

`observe()`는 업무 풀과 독립된 풀 크기 1/overflow 0의 관측 연결을 사용한다.
`pg_stat_activity`의 PID·대기·차단 PID·트랜잭션 시각·SQL hash와 `pg_locks`를
읽고, SQL timeout 2초/전체 수집 timeout 5초로 제한한다. 요청이 끝나지 않아도
관측은 계속된다. 종료 시 호출자는 먼저 부하·관측 task를 중단하고 `dispose()`한다.
획득·반환·SQL 이벤트의 `backend_pid`를 DB 대기 스냅샷의 PID와 연결할 수 있다.
업무 SQL hash는 SHA-256, PostgreSQL 내부 대기 query hash는 MD5이며
`sql_hash_algorithm`으로 구분한다. 서로 다른 알고리즘의 hash를 직접 비교하지 않는다.
DB 연결 상한과 현재 DB 연결 수도 스냅샷에 포함한다.

Settings 기본값은 `db_pool_timeout_seconds=30`,
`db_statement_timeout_ms=0`, `db_observability_enabled=False`,
`db_observability_interval_seconds=5`다. 기본 업무 풀 5/overflow 10은 유지한다.

## 로컬 실행

전용 PostgreSQL은 호출자가 준비하고 수명도 관리한다. 이 runner는 Docker를
시작·종료하지 않는다. 예제 env 파일에는 `DATABASE_URL` 한 줄만 둔다.
자격 증명을 명령줄 인자로 넘기거나 출력하지 않는다.

```sh
uv run python demo/local_runner.py \
  --env-file /path/to/owned-postgres.env \
  --expected-port 32768 \
  --output /private/tmp/healthcare-proof-new
```

출력 디렉터리는 새 경로여야 한다. runner는 `127.0.0.1`, 명시한 포트,
`rca_demo`, `postgresql+asyncpg`만 허용하고 **5432는 항상 거부**한다.
고유 `proof_local_*` 스키마를 만들고 모든 업무 연결의 search_path를 그 스키마로
고정한다. 정상→결함→복원마다 새 프로세스와 독립적으로 빌드된 Python 소스를 쓴다.
SQL 계측은 물리 연결 워밍업 뒤 시작한다.

실행하는 실험:

1. 조회: 고정된 120행을 각 구간에서 5회 조회. 같은 응답 해시, SQL `1→121→1` 확인.
2. 풀: 같은 8개 동시 요청 × 1,000행 INSERT, timeout 20ms. 풀 크기만 `8→1→8`,
   overflow는 모두 0. 정상·복원 전부 성공하고 결함에서 풀 timeout이 있어야 통과.
3. 락: 별도 CLI의 backend와 실제 차단 관계를 관측. 저장 statement timeout 500ms.
   SIGTERM 롤백 후 저장 성공, 별도의 짧은 제한 시간 자동 해제도 확인.
4. 세션: 음수 LIMIT 3회 후 정상 쓰기. 풀 크기 3에서 점유 `0→[1,2,3]→0` 및
   정상 쓰기의 실패·복원 확인. 별도 취소 probe도 같은 리비전 순서로 검사.

위 숫자는 **로컬 메커니즘 검증 조건**이다. AWS 부하량·알람 임계치가 아니다.
실험 데이터의 일부 값은 정상 바이탈로 고정해 불필요한 도메인 경고를 줄인다.
서비스 배경 부하의 이상치 비율은 별도 구성 경로다.

`local-query-calibration.json`에는 로컬 조회 지연의 후보 임계치와 구간별 비교를 남긴다.
같은 실행의 정상·복원 최대 지연과 결함 최소 지연 사이의 중간값을 사용하며,
정상·복원 모든 샘플이 미만, 결함 모든 샘플이 초과여야 통과한다.
샘플이 겹치면 숫자를 바꾸지 않고 실패한다. 원본 `elapsed_ms`는 그대로 보존한다.
이 값은 **이 PostgreSQL fixture와 동일 조회 입력에만 적용하는 같은 실행 기반 보정**이다.
독립 데이터 검증, AWS 기본 500ms의 보정, CloudWatch 알람 발화를 뜻하지 않는다.
AWS 임계치·알람 보정은 별도 배포 검증에 남겨 둔다.

각 phase JSON과 종합 `evidence.json`에 소스 manifest, 실제 SQL·연결 관측,
응답 hash, 오류 타입, 락 관계와 정리 결과가 남는다. 서비스 메서드와 실제
PostgreSQL을 실행하지만 HTTP/CloudWatch/모델 평가를 이 runner가 대신하지 않는다.
시간 정렬에는 실행 중 직접 기록한 UTC ISO 8601 필드를 사용한다.
`started_at`/`completed_at`은 worker 전체 범위,
`measurement_started_at`/`measurement_completed_at`은 워밍업·종료 정리를 제외한
측정 범위다. 각 측정 작업에도 `started_at`/`completed_at`이 있고,
로그 `timestamp`는 로그 발생 시각이다. 종합 JSON의 `phase_windows`와
각 phase의 `process_window`는 부모가 기록한 프로세스 시작 시도부터 그룹 정리까지의
범위다. 종합 JSON의 시작·종료 시각은 전체 실행과 최종 자원 정리를 포함한다.
예전 실행에 시각을 추가하거나 기간을 추정해 채우지 않는다.

runner는 소스·overlay·worker를 두 번 읽어 캡처 중 변경이 없는지 확인한 뒤
하나의 임시 스냅샷에 고정한다. r1/r2/r3는 모두 이 스냅샷에서 빌드하고 worker도
스냅샷의 파일로 실행한다. `source_snapshot`은 캡처 시각과 파일별 hash를,
각 리비전의 `source.base_fingerprint`는 overlay 적용 전 공통 소스 hash를 제공한다.
캡처 후 작업공간이 변경되어도 세 리비전의 공통 기반은 바뀌지 않는다.
`finally`에서 소유 프로세스·연결·스키마를 정리하고 스키마 부재를 확인한다.
소유 스키마 외 테이블/사용자/컨테이너를 수정하지 않는다.
각 단계가 끝날 때 부모 프로세스의 종료 여부와 별개로 그 단계가 만든 프로세스 그룹
전체를 정리한다. 남은 자식에는 TERM을 보내 15초까지 기다리고, 이후 KILL과
5초 확인 한도를 적용한다. 출력 파이프 정리도 5초로 제한한다. 성공적으로 정리한
그룹 ID는 즉시 제거하며, 실패한 그룹은 마지막 정리에서 다시 시도한다.
발생한 정리 오류는 JSON에 보존하고 연결·스키마·임시 소스 정리는 각각 계속한다.

```sh
uv run pytest -q
HEALTHCARE_PROOF_ENV_FILE=/path/to/owned-postgres.env \
  uv run pytest tests/test_real_mechanisms.py -q
```
