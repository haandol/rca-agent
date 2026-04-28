from __future__ import annotations

from abc import ABC, abstractmethod


class EvidenceStorePort(ABC):
    @abstractmethod
    def save(self, rca_id: str, hypothesis_id: str, evidence_text: str) -> str | None: ...
