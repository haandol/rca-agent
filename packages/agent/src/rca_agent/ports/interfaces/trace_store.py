from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rca_agent.ports.dto.models import Hypothesis


class TraceStorePort(ABC):
    @abstractmethod
    def start_span(self, span_type, *, parent_span_id=None, loop_index=None, input_summary=""): ...

    @abstractmethod
    def end_span(self, span, *, output_summary="", status=None, error=None, metadata=None) -> None: ...

    @abstractmethod
    @contextmanager
    def span(self, span_type, *, parent_span_id=None, loop_index=None, input_summary="") -> Generator: ...

    @abstractmethod
    def put_hypotheses(self, hypotheses: list[Hypothesis]) -> None: ...

    @abstractmethod
    def update_hypothesis_status(
        self,
        hypothesis_id: str,
        *,
        status: str,
        confidence: float | None = None,
        judgment_reasoning: str = "",
    ) -> None: ...

    @abstractmethod
    def update_hypothesis_evidence(self, hypothesis_id: str, *, evidence_summary: str) -> None: ...

    @abstractmethod
    def check_cancelled(self) -> None: ...
