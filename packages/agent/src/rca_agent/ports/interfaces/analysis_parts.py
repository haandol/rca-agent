"""Storage capability for private analysis parts, independent of a concrete AWS adapter."""

from __future__ import annotations

from typing import Protocol


class AnalysisPartStorePort(Protocol):
    """Keep callers dependent on immutable source and publication behavior, not an adapter class."""

    def read_incident(self, rca_id: str) -> dict | None:
        """Return verified original incident bytes or an explicit absence before first freeze."""
        ...

    def freeze_incident(self, rca_id: str, claim_token: str, attempt: int, incident: dict) -> dict:
        """Freeze one engine-neutral original under current ownership before early publication."""
        ...

    def read_part(self, rca_id: str, part: str) -> dict | None:
        """Return verified stage content without falling back to a mutable public runbook."""
        ...

    def start_part(self, rca_id: str, part: str, claim_token: str, attempt: int) -> dict:
        """Register actual work only after its predecessor result is durable."""
        ...

    def publish_part(self, rca_id: str, part: str, claim_token: str, attempt: int, **kwargs) -> dict:
        """Persist the body before claim-fenced authority and preserve immutable revisions."""
        ...

    def complete_analysis(self, rca_id: str, claim_token: str, **kwargs) -> bool:
        """Finish globally only after all part results, retaining final public handoff separately."""
        ...
