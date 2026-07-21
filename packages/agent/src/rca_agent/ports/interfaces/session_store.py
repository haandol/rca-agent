from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import (
    AlarmPayload,
    CompletionHandoff,
    FaultType,
    NotificationMessage,
    RcaSession,
    RcaSessionState,
    RemediationContext,
    RemediationResult,
    VerificationResult,
)


class SessionStorePort(ABC):
    @abstractmethod
    def check_duplicate(self, alarm: AlarmPayload) -> bool: ...

    @abstractmethod
    def create_session(self, alarm: AlarmPayload) -> RcaSession | None: ...

    @abstractmethod
    def update_state(self, rca_id: str, new_state: RcaSessionState) -> bool: ...

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
    ) -> bool: ...

    @abstractmethod
    def mark_failed(self, rca_id: str, *, error_reason: str = "") -> bool: ...

    @abstractmethod
    def mark_outdated(self, rca_id: str, *, reason: str = "") -> bool: ...

    @abstractmethod
    def get_completion_handoff(self, rca_id: str) -> CompletionHandoff | None: ...

    @abstractmethod
    def mark_completion_notified(self, rca_id: str) -> bool: ...

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
    ) -> bool: ...

    @abstractmethod
    def release_remediation(self, rca_id: str, claim_token: str, *, error_reason: str) -> bool: ...
