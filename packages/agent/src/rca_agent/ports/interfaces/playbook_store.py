from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import Playbook, PlaybookMatch, ScopingResult


class PlaybookStorePort(ABC):
    @abstractmethod
    def search_similar(self, query_text: str, *, threshold: float) -> list[PlaybookMatch]:
        """Return hits at or above ``threshold`` similarity.

        The threshold is explicit because it is a property of the caller's intent,
        not of the index: merging demands a stricter cutoff than plain retrieval.
        """

    @abstractmethod
    def load_detail(self, match: PlaybookMatch) -> Playbook | None:
        """Load the full playbook behind a search hit, or None if unavailable.

        The vector index only keeps lightweight metadata, so the detail fields a
        merge needs live elsewhere and may already have expired.
        """

    @abstractmethod
    def save(self, playbook: Playbook, *, scoping_result: ScopingResult | None = None) -> bool: ...
