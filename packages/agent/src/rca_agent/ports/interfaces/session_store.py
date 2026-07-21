from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from rca_agent.ports.dto.models import (
    AlarmPayload,
    CompletionHandoff,
    FaultType,
    NotificationMessage,
    RcaSession,
    RcaSessionState,
    RemediationContext,
    RemediationHandoff,
    RemediationResult,
    VerificationResult,
)


class ClaimDisposition(StrEnum):
    CLAIMED = "CLAIMED"
    TERMINAL_DUPLICATE = "TERMINAL_DUPLICATE"
    CONTENDED = "CONTENDED"


@dataclass(frozen=True)
class SessionClaim:
    disposition: ClaimDisposition
    claim_token: str | None = None
    attempt: int | None = None

    @property
    def acquired(self) -> bool:
        return self.disposition is ClaimDisposition.CLAIMED and self.claim_token is not None


class SessionOwnershipCheckError(RuntimeError):
    pass


class SideEffectLeaseUnavailableError(SessionOwnershipCheckError):
    pass


class SessionStorePort(ABC):
    @abstractmethod
    def check_duplicate(self, alarm: AlarmPayload) -> bool: ...

    @abstractmethod
    def create_session(self, alarm: AlarmPayload) -> RcaSession | None: ...

    @abstractmethod
    def claim_session(
        self,
        alarm: AlarmPayload,
        *,
        receive_count: int,
        message_id: str | None = None,
    ) -> SessionClaim: ...

    @abstractmethod
    def acquire_side_effect_lease(
        self,
        rca_id: str,
        claim_token: str,
        effect_name: str,
        *,
        lease_seconds: int,
    ) -> str: ...

    @abstractmethod
    def release_side_effect_lease(
        self,
        rca_id: str,
        claim_token: str,
        lease_token: str,
    ) -> bool: ...

    @abstractmethod
    def update_state(
        self,
        rca_id: str,
        new_state: RcaSessionState,
        *,
        claim_token: str | None = None,
    ) -> bool: ...

    @abstractmethod
    def mark_completed(
        self,
        rca_id: str,
        *,
        root_cause: str = "",
        confirmed: bool = False,
        selected_hypothesis_id: str = "",
        fault_type: FaultType = FaultType.UNSUPPORTED,
        completion_notification: NotificationMessage | None = None,
        report_s3_key: str = "",
        claim_token: str | None = None,
    ) -> bool: ...

    @abstractmethod
    def mark_failed(
        self,
        rca_id: str,
        *,
        error_reason: str = "",
        claim_token: str | None = None,
    ) -> bool: ...

    @abstractmethod
    def mark_outdated(
        self,
        rca_id: str,
        *,
        reason: str = "",
        claim_token: str | None = None,
    ) -> bool: ...

    @abstractmethod
    def get_completion_handoff(self, rca_id: str) -> CompletionHandoff | None: ...

    @abstractmethod
    def mark_completion_notified(self, rca_id: str, *, claim_token: str | None = None) -> bool: ...

    @abstractmethod
    def get_remediation_context(self, rca_id: str) -> RemediationContext | None: ...

    @abstractmethod
    def claim_remediation(self, rca_id: str) -> str | None: ...

    @abstractmethod
    def complete_remediation(
        self,
        rca_id: str,
        claim_token: str,
        result: RemediationResult,
        verification: VerificationResult | None,
        notification: NotificationMessage,
    ) -> bool: ...

    @abstractmethod
    def release_remediation(self, rca_id: str, claim_token: str, *, error_reason: str) -> bool: ...

    @abstractmethod
    def get_remediation_handoff(self, rca_id: str) -> RemediationHandoff | None: ...

    @abstractmethod
    def claim_remediation_publication(self, rca_id: str, *, lease_seconds: int) -> str | None: ...

    @abstractmethod
    def mark_remediation_published(self, rca_id: str, publication_claim_token: str) -> bool: ...

    @abstractmethod
    def release_remediation_publication(self, rca_id: str, publication_claim_token: str) -> bool: ...
