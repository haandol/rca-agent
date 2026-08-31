from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from headless_codex.ports.dto.models import CodexResult


class ExecutionRunnerPort(ABC):
    """플레이북 실행과 회고를 수행하는 CC 하네스.

    분석 하네스와 별도다. 실행 하네스만 쓰기 도구를 갖고, 분석 하네스는 읽기 전용을
    유지한다. 두 하네스가 도구 목록을 공유하면 분석 경로의 결함이 쓰기 권한에 닿는다.
    """

    @abstractmethod
    def run_execution(
        self,
        prompt: str,
        *,
        execution_token: str,
        execution_id: str,
        approved_step_ids: tuple[str, ...],
        approved_success_criteria: dict[str, str],
        cancel_checker: Callable[[], bool] | None = None,
    ) -> CodexResult: ...

    @abstractmethod
    def run_retrospective(
        self,
        prompt: str,
        *,
        execution_token: str,
        execution_id: str,
    ) -> CodexResult: ...
