from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import RcaReport, ReportMatch, ScopingResult


class ReportStorePort(ABC):
    @abstractmethod
    def save(self, report: RcaReport) -> str: ...

    @abstractmethod
    def search_similar(self, query_text: str) -> list[ReportMatch]: ...

    @abstractmethod
    def save_vectors(self, report: RcaReport, *, scoping_result: ScopingResult | None = None) -> bool: ...
