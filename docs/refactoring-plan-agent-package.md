# agent 패키지 리팩토링 계획

## 현재 상태 진단

### 파일별 라인 수 (상위)

| 파일 | 줄 수 | 역할 |
|------|-------|------|
| `main.py` | 914 | SQS 진입점 + _Agents + _process_alarm + _run_pipeline(레거시) + re-export |
| `services/pipeline.py` | 679 | PipelineOrchestrator(DI 기반, 신규) |
| `trace_store.py` | 496 | DynamoDB 트레이스 저장 |
| `prompts.py` | 409 | 9개 스테이지 LLM 프롬프트 |
| `playbook_gen.py` | 335 | 플레이북 생성 로직 |
| `evidence.py` | 312 | 증거 수집 오케스트레이션 |
| `session_store.py` | 311 | 세션 라이프사이클 (DynamoDB) |
| `agent_factory.py` | 206 | MCP 클라이언트/에이전트 팩토리 |
| `report.py` | 187 | RCA 리포트 생성 |
| `scoping.py` | 185 | 초기 스코핑 |
| `notification.py` | 108 | SNS 알림 |

### 핵심 문제 5가지

1. **main.py/pipeline.py 이중 구현**: 동일한 9-stage 파이프라인 로직이 `main.py`(249~807줄)와 `services/pipeline.py`(160~679줄) 양쪽에 존재
2. **코드 위치 역전**: 실제 비즈니스 로직이 루트(`rca_agent/scoping.py` 등)에 있고, `services/*`는 이를 re-export하는 8~15줄짜리 래퍼
3. **main.py 하위호환 re-export**: 1~48줄이 테스트 패치용 `from rca_agent.main import X` re-export로 가득
4. **인프라 모듈이 루트에 위치**: `trace_store.py`, `session_store.py`가 루트에 있으나 adapter 계층(`adapters/secondary/session/`)도 별도 존재
5. **pipeline.py `_run_pipeline`이 ~520줄 단일 메서드**: 9개 스테이지 전체가 한 메서드에 들어있어 가독성 저하

### 현재 디렉토리 구조

```
packages/agent/src/rca_agent/
├── main.py                          # 914줄 — SQS 폴링 + 레거시 파이프라인 + re-export
├── scoping.py                       # 185줄 — 실제 구현
├── evidence.py                      # 312줄 — 실제 구현
├── report.py                        # 187줄 — 실제 구현
├── playbook_gen.py                  # 335줄 — 실제 구현
├── notification.py                  # 108줄 — 실제 구현
├── session_store.py                 # 311줄 — 실제 구현
├── trace_store.py                   # 496줄 — 실제 구현
├── prompts.py                       # 409줄 — 프롬프트 상수
├── agent_factory.py                 # 206줄
├── embeddings.py                    # 18줄
├── models.py                        # 1줄 — from ports.dto.models import *
├── hypothesis.py                    # 11줄 — re-export
├── prioritization.py                # 10줄 — re-export
├── validation.py                    # 9줄 — re-export
├── branching.py                     # 9줄 — re-export
├── termination.py                   # 5줄 — re-export
├── remediation.py                   # 7줄 — re-export
├── verification.py                  # 6줄 — re-export
├── healthz.py
├── config.py
├── config/
│   └── settings.py
├── di/
│   ├── container.py                 # Container ABC
│   └── app_container.py             # AppContainer (DI 구현)
├── ports/
│   ├── dto/models.py                # 264줄 — Pydantic DTO
│   └── interfaces/                  # 포트 인터페이스 (SessionStorePort 등)
├── adapters/
│   ├── primary/
│   │   ├── health/health_server.py
│   │   └── sqs/__init__.py          # 비어있음
│   └── secondary/
│       ├── session/dynamodb_session_store.py   # 259줄
│       ├── playbook/s3_vectors_playbook_store.py
│       ├── report/s3_report_store.py
│       ├── evidence/s3_evidence_store.py
│       ├── notification/sns_notification.py
│       ├── embedding/bedrock_embedding.py
│       └── queue/sqs_consumer.py
└── services/
    ├── pipeline.py                  # 679줄 — PipelineOrchestrator (신규 DI)
    ├── scoping.py                   # 8줄 — re-export from rca_agent.scoping
    ├── evidence.py                  # 14줄 — re-export from rca_agent.evidence
    ├── report.py                    # 8줄 — re-export from rca_agent.report
    ├── playbook_gen.py              # 15줄 — re-export from rca_agent.playbook_gen
    ├── hypothesis.py                # 164줄 — 실제 구현
    ├── prioritization.py            # 121줄 — 실제 구현
    ├── validation.py                # 145줄 — 실제 구현
    ├── branching.py                 # 120줄 — 실제 구현
    ├── termination.py               # 89줄 — 실제 구현
    ├── remediation.py               # 134줄 — 실제 구현
    └── verification.py              # 101줄 — 실제 구현
```

### 의존 그래프 요약

- **main.py** → `services/pipeline.py`의 유틸 함수 import (parse_sns_envelope, should_process 등)
- **main.py** → 루트 모듈에서 re-export (테스트 패치용)
- **services/pipeline.py** → `services/*` 개별 스테이지 함수 import
- **services/pipeline.py** → `trace_store.py`, `notification.py` 직접 import
- **tests/test_main.py** → `rca_agent.main.*` 패치 (17개 함수)
- **tests/test_state_machine.py** → `rca_agent.session_store.*`, `rca_agent.evidence.*` import

---

## Phase 1: main.py 축소 — 레거시 파이프라인 제거 (완료)

### 목표

`main.py` 914줄 → ~80줄. 순수 진입점(SQS 폴링 루프)만 남기고, 파이프라인 로직은 `PipelineOrchestrator`로 단일화.

### 작업 내역

#### 1-1. SQS 폴링 루프를 primary adapter로 이동

**현재**: `main.py:809~914`에 `main()` 함수가 SQS 폴링 루프를 직접 구현

**변경**:
- `adapters/primary/sqs/sqs_consumer.py` 생성 — `QueueConsumerPort` 구현
- `main()` 함수 본체를 이 adapter로 이동
- 참고: `adapters/secondary/queue/sqs_consumer.py`가 이미 존재하는지 확인 필요. 기존 `AppContainer.queue_consumer`가 `adapters/secondary/queue/sqs_consumer.py`를 참조하므로, 이것과 중복되지 않도록 정리

**main.py 최종 형태**:
```python
import logging
import os
import signal
import sys

from rca_agent.di.app_container import AppContainer
from rca_agent.services.pipeline import PipelineOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_running = True

def _handle_signal(signum, _frame):
    global _running
    logger.info("Received signal %s, shutting down", signum)
    _running = False

def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    queue_url = os.environ.get("SQS_QUEUE_URL", "")
    if not queue_url:
        logger.error("SQS_QUEUE_URL is not set")
        sys.exit(1)

    from rca_agent.adapters.primary.health.health_server import start_health_server
    start_health_server()

    container = AppContainer(queue_url)
    orchestrator = PipelineOrchestrator(container)
    consumer = container.queue_consumer

    logger.info("Starting SQS long polling: %s", queue_url)
    consumer.poll(orchestrator.process_alarm, running=lambda: _running)
    logger.info("Shutdown complete")

if __name__ == "__main__":
    main()
```

#### 1-2. `_Agents` 클래스 삭제

**현재**: `main.py:68~144` — lazy agent holder, `_process_alarm`에 주입
**이유**: `AppContainer`(di/app_container.py)가 동일한 lazy agent 생성을 이미 담당
**변경**: `_Agents` 클래스 삭제. `_process_alarm`을 호출하는 코드는 `PipelineOrchestrator`로 대체

#### 1-3. `_process_alarm()` + `_run_pipeline()` 삭제

**현재**: `main.py:147~807` — 레거시 파이프라인. `services/pipeline.py:79~679`의 `PipelineOrchestrator`가 동일 로직을 DI 기반으로 구현
**변경**: 두 함수 모두 삭제. `PipelineOrchestrator.process_alarm()`이 유일한 파이프라인 진입점

#### 1-4. 하위호환 re-export 삭제 + 테스트 import 수정

**현재**: `main.py:1~48`의 re-export:
```python
from rca_agent.services.branching import run_branching  # noqa: F401
from rca_agent.services.evidence import run_evidence_collection  # noqa: F401
...
from rca_agent.session_store import check_duplicate, create_session, ...  # noqa: F401
```

**영향받는 테스트 파일**:

| 테스트 파일 | 현재 import | 변경 후 import |
|-------------|------------|---------------|
| `test_main.py` | `from rca_agent.main import _Agents, _parse_sns_envelope, _process_alarm, _prune_subtree` | 삭제 후 `PipelineOrchestrator` 기반으로 재작성 |
| `test_main.py` | `from rca_agent.evidence import EvidenceCollectionSummary` | 유지 (루트 모듈은 Phase 2에서 정리) |
| `test_main.py` | `from rca_agent.models import ...` | 유지 |
| `test_main.py` | `from rca_agent.session_store import SessionCancelledError` | 유지 |
| `test_state_machine.py` | `from rca_agent.main import _process_alarm` (375줄) | `PipelineOrchestrator.process_alarm` 으로 변경 |

**test_main.py 변경 전략**:
- `TestParseSnsEnvelope`: `parse_sns_envelope`를 `services.pipeline`에서 import하도록 변경
- `TestProcessAlarmFullPipeline`: `PipelineOrchestrator`를 테스트하도록 재작성. mock 대상을 `rca_agent.services.pipeline.*` 경로로 변경
- `TestPruneSubtree`: `prune_subtree`를 `services.pipeline`에서 import하도록 변경

#### 1-5. QueueConsumerPort에 poll 메서드 추가

**현재**: `ports/interfaces/queue_consumer.py`에 `QueueConsumerPort` 인터페이스 존재
**변경**: `poll(callback, running)` 메서드가 없다면 추가. `adapters/secondary/queue/sqs_consumer.py`에서 기존 `main()` SQS 루프 로직 통합

### 실행 순서

```
1-5. QueueConsumerPort.poll() 추가 → SqsConsumer에 구현
  ↓
1-1. main.py를 ~80줄 진입점으로 재작성
  ↓
1-2 + 1-3. _Agents, _process_alarm, _run_pipeline 삭제 (1-1에 포함)
  ↓
1-4. test_main.py, test_state_machine.py import 수정 + PipelineOrchestrator 기반으로 재작성
  ↓
테스트 실행 및 검증
```

### 검증 체크리스트

- [ ] `pytest packages/agent/tests/` 전체 통과
- [ ] `main.py` 100줄 이하
- [ ] `_Agents`, `_process_alarm`, `_run_pipeline` 완전 삭제
- [ ] `rca_agent.main.*` 패치가 테스트에 남아있지 않음
- [ ] `PipelineOrchestrator`가 유일한 파이프라인 진입점

### 주의사항

- Dockerfile의 entrypoint가 `main:main` 등을 참조할 수 있으므로 확인 필요
- `pyproject.toml`의 `[project.scripts]` 또는 `console_scripts` 확인
- Phase 1에서는 루트의 `scoping.py`, `evidence.py` 등은 건드리지 않음 (Phase 2 범위)

---

## Phase 2: 비즈니스 로직을 services/로 이동 (완료)

루트의 `scoping.py`(185줄), `evidence.py`(312줄), `report.py`(187줄), `playbook_gen.py`(335줄), `notification.py`(108줄) 실제 구현을 `services/*`로 이동. 루트 파일은 `services/*`에서 re-export하는 얇은 스텁(총 49줄)으로 전환. 테스트 import 및 `@patch` 경로를 `rca_agent.services.*`로 일괄 변경. `pipeline.py`의 `notification` inline import도 `services.notification`으로 변경.

## Phase 3: 인프라 모듈을 adapters/로 통합 (완료)

`trace_store.py`(496줄) → `adapters/secondary/trace/dynamodb_trace_store.py`로 이동. `session_store.py`(311줄)의 함수 기반 코드를 `adapters/secondary/session/dynamodb_session_store.py`에 병합. 루트 파일은 adapter에서 re-export하는 스텁(총 28줄)으로 전환. `pipeline.py`의 `trace_store` import를 adapter 경로로 변경.

## Phase 4: pipeline.py `_run_pipeline` 분할 (완료)

520줄 단일 메서드를 5개 메서드로 분할: `_run_scoping`, `_run_hypothesis_generation`, `_run_validation_loop`, `_finalize_hypotheses`, `_run_report_and_notify`. `_run_pipeline`은 이 메서드들을 순서대로 호출하는 ~30줄 오케스트레이션 메서드로 축소.

## Phase 5 (선택): prompts.py 분리 (예정)

409줄의 프롬프트 상수를 `prompts/` 패키지로 분리하거나 각 서비스 모듈 내로 이동.
