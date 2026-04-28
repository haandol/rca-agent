from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import AlarmPayload, RcaSession, RcaSessionState


class SessionStorePort(ABC):
    @abstractmethod
    def check_duplicate(self, alarm: AlarmPayload) -> bool: ...

    @abstractmethod
    def create_session(self, alarm: AlarmPayload) -> RcaSession | None: ...

    @abstractmethod
    def update_state(self, rca_id: str, new_state: RcaSessionState) -> bool: ...

    @abstractmethod
    def mark_completed(self, rca_id: str, *, root_cause: str = "", confirmed: bool = False) -> bool: ...

    @abstractmethod
    def mark_failed(self, rca_id: str, *, error_reason: str = "") -> bool: ...

    @abstractmethod
    def mark_outdated(self, rca_id: str, *, reason: str = "") -> bool: ...
