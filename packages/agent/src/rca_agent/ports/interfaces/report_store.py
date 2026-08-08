from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import Playbook, RcaReport, ReportMatch, ScopingResult


class ReportStorePort(ABC):
    @abstractmethod
    def save(
        self,
        report: RcaReport,
        *,
        playbook: Playbook | None,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> str:
        """Persist the report with its playbook as one artifact.

        The argument is explicit rather than defaulted: a report a person approves
        has to show the procedure that will run, so omitting it must be a decision
        the caller states. None means playbook generation failed, and the report
        still has to reach the person — the analysis result is the deliverable and
        the playbook is an asset for later. The narrative then says no procedure
        exists instead of implying one that was never written.
        """

    @abstractmethod
    def search_similar(self, query_text: str) -> list[ReportMatch]:
        """Return reports similar enough to inform hypothesis generation.

        Takes no threshold, unlike the playbook store: there is one caller and
        one intent here, so the cutoff stays a property of the index.
        """

    @abstractmethod
    def save_vectors(self, report: RcaReport, *, scoping_result: ScopingResult | None = None) -> bool: ...
