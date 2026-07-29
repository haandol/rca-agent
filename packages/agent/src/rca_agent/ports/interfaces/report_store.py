from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import RcaReport, ReportMatch, ScopingResult


class ReportStorePort(ABC):
    @abstractmethod
    def save(
        self,
        report: RcaReport,
        *,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> str: ...

    @abstractmethod
    def search_similar(self, query_text: str) -> list[ReportMatch]:
        """Return reports similar enough to inform hypothesis generation.

        Takes no threshold, unlike the playbook store: there is one caller and
        one intent here, so the cutoff stays a property of the index.
        """

    @abstractmethod
    def save_vectors(self, report: RcaReport, *, scoping_result: ScopingResult | None = None) -> bool: ...
