"""플레이북 실행의 상태와 허용 전이.

실행은 분석 세션과 별도 생명주기를 가진다. 실행 실패가 분석 세션을 실패로 만들지
않으며 이미 저장된 리포트를 변경하지 않는다. 이 모듈이 그 생명주기의 단일 소스다.
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionState(StrEnum):
    # 승인 요청이 큐에 발행되었고 아직 워커가 집지 않은 상태.
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# 실행 중에서 해결로 바로 가는 경로는 없다 — 관측 없이 해결로 전이하면 미해결 장애가
# 완료로 기록된다.
VALID_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.PENDING_APPROVAL: frozenset(
        {ExecutionState.EXECUTING, ExecutionState.CANCELLED, ExecutionState.FAILED}
    ),
    ExecutionState.EXECUTING: frozenset(
        {
            ExecutionState.VERIFYING,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
        }
    ),
    ExecutionState.VERIFYING: frozenset(
        {
            ExecutionState.RESOLVED,
            ExecutionState.UNRESOLVED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
        }
    ),
}

TERMINAL_STATES: frozenset[ExecutionState] = frozenset(
    {
        ExecutionState.RESOLVED,
        ExecutionState.UNRESOLVED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
    }
)

# 회고는 해결된 실행만 입력으로 받는다. 해소되지 않은 실행의 절차는 올바름이
# 입증되지 않았으므로 검증된 절차로 승격하면 플레이북이 나빠진다.
RETROSPECTIVE_ENTRY_STATE = ExecutionState.RESOLVED


class InvalidExecutionTransitionError(Exception):
    pass


def parse_state(value: object) -> ExecutionState | None:
    if not isinstance(value, str):
        return None
    try:
        return ExecutionState(value)
    except ValueError:
        return None


def is_terminal(state: ExecutionState) -> bool:
    return state in TERMINAL_STATES


def assert_transition(current: ExecutionState, target: ExecutionState) -> None:
    if target not in VALID_TRANSITIONS.get(current, frozenset()):
        raise InvalidExecutionTransitionError(f"{current} → {target}")


def enters_retrospective(state: ExecutionState) -> bool:
    return state is RETROSPECTIVE_ENTRY_STATE
