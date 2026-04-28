from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import RcaReport


class ReportStorePort(ABC):
    @abstractmethod
    def save(self, report: RcaReport) -> str: ...
