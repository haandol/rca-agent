from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from cc_headless.ports.dto.models import CcResult


class CcRunnerPort(ABC):
    @abstractmethod
    def run(
        self,
        prompt: str,
        *,
        execution_token: str,
        mcp_config: str | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        rca_id: str | None = None,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> CcResult: ...
