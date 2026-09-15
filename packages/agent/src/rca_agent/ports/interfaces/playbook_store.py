from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import Playbook, PlaybookMatch, ScopingResult


class PlaybookArchiveUnavailable(RuntimeError):  # noqa: N818 - shared port name
    """An immutable comparison original could not be persisted; callers must retain a thin failure state."""


class PlaybookSearchUnavailable(RuntimeError):  # noqa: N818 - shared port name
    """Search infrastructure failed; callers must record SEARCH_FAILED, not no candidates."""


class PlaybookStorePort(ABC):
    @abstractmethod
    def search_similar(self, query_text: str, *, threshold: float) -> list[PlaybookMatch]:
        """Return hits at or above ``threshold`` similarity.

        Raise PlaybookSearchUnavailable for embedding/index failure; retain unavailable candidate identities.
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
    def save(
        self,
        playbook: Playbook,
        *,
        scoping_result: ScopingResult | None = None,
        metric_name: str = "",
    ) -> bool:
        """Publish completed unmatched knowledge; matched comparisons remain incident-only."""

    def archive_comparison(self, playbook: Playbook, rca_id: str, engine: str) -> Playbook:
        """Archive full inputs before completion and return only their immutable reference in comparison."""
        raise PlaybookArchiveUnavailable("comparison archive is not supported")
