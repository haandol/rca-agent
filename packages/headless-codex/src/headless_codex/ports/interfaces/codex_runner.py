from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from headless_codex.ports.dto.models import CodexResult


class CodexRunnerPort(ABC):
    @abstractmethod
    def run(
        self,
        prompt: str,
        *,
        execution_token: str,
        profile: str = "analysis",
        report_prompt: str | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        rca_id: str | None = None,
        claim_token: str | None = None,
        attempt: int | None = None,
        deadline: float | None = None,
        analysis_parts=None,
    ) -> CodexResult:
        """Carry caller-owned identity, cancellation and a shared deadline across profile execution."""
        ...

    def compare_playbooks(
        self,
        payload: dict,
        *,
        execution_token: str,
        deadline: float,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict:
        """Return a validated judgment without writing analysis artifacts."""
        raise NotImplementedError("This runner does not support playbook comparison")
