from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import Playbook, PlaybookMatch, ScopingResult


class PlaybookStorePort(ABC):
    @abstractmethod
    def search_similar(self, query_text: str) -> list[PlaybookMatch]: ...

    @abstractmethod
    def save(self, playbook: Playbook, *, scoping_result: ScopingResult | None = None) -> bool: ...
