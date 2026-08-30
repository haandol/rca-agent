from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from codex_headless.ports.dto.models import CodexResult


class CodexRunnerPort(ABC):
    @abstractmethod
    def run(
        self,
        prompt: str,
        *,
        execution_token: str,
        profile: str = "analysis",
        cancel_checker: Callable[[], bool] | None = None,
        rca_id: str | None = None,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> CodexResult: ...
