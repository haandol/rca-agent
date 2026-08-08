from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ClaimDisposition(StrEnum):
    CLAIMED = "CLAIMED"
    TERMINAL_DUPLICATE = "TERMINAL_DUPLICATE"
    CONTENDED = "CONTENDED"


class IncidentClaimDisposition(StrEnum):
    PROCEED = "PROCEED"
    SUPPRESSED = "SUPPRESSED"
    CONTENDED = "CONTENDED"


@dataclass(frozen=True)
class IncidentAlarm:
    alarm_name: str
    alarm_arn: str | None = None
    region: str = "us-east-1"
    state_change_time: datetime | None = None
    new_state: str = "ALARM"


@dataclass(frozen=True)
class IncidentClaim:
    disposition: IncidentClaimDisposition
    candidate_rca_id: str
    generation: int | None = None
    reason: str = ""
    retryable: bool = False

    @property
    def acquired(self) -> bool:
        return self.disposition is IncidentClaimDisposition.PROCEED


@dataclass(frozen=True)
class SessionClaim:
    disposition: ClaimDisposition
    claim_token: str | None = None
    attempt: int | None = None

    @property
    def acquired(self) -> bool:
        return self.disposition is ClaimDisposition.CLAIMED and self.claim_token is not None


class SessionCancelledError(RuntimeError):
    pass


class SessionOwnershipCheckError(RuntimeError):
    pass


class SideEffectLeaseUnavailableError(RuntimeError):
    pass


class SessionStorePort(ABC):
    @abstractmethod
    def claim_incident(
        self,
        alarm: IncidentAlarm,
        *,
        cooldown_seconds: int,
    ) -> IncidentClaim: ...

    @abstractmethod
    def record_recovery(self, alarm: IncidentAlarm) -> bool: ...

    @abstractmethod
    def claim_session(
        self,
        rca_id: str,
        alarm_name: str,
        idempotency_key: str,
        *,
        receive_count: int,
        alarm_data: dict | None = None,
    ) -> SessionClaim: ...

    @abstractmethod
    def update_state(self, rca_id: str, state: str, *, claim_token: str) -> None: ...

    @abstractmethod
    def mark_completed(
        self,
        rca_id: str,
        root_cause: str,
        report_s3_key: str,
        *,
        playbook: dict | None = None,
        confirmed: bool = False,
        claim_token: str,
        side_effect_lease_token: str | None = None,
    ) -> None: ...

    @abstractmethod
    def mark_failed(self, rca_id: str, error_reason: str, *, claim_token: str) -> None: ...

    @abstractmethod
    def mark_outdated(self, rca_id: str, reason: str, *, claim_token: str) -> None: ...

    @abstractmethod
    def is_terminated(self, rca_id: str, *, claim_token: str) -> bool: ...

    @abstractmethod
    def acquire_side_effect_lease(
        self,
        rca_id: str,
        *,
        claim_token: str,
        effect_name: str,
        lease_seconds: int,
    ) -> str: ...

    @abstractmethod
    def release_side_effect_lease(
        self,
        rca_id: str,
        *,
        claim_token: str,
        lease_token: str,
    ) -> None: ...
