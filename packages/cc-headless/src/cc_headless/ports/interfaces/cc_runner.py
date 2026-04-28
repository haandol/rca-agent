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
        mcp_config: str | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> CcResult: ...
