from __future__ import annotations

from abc import ABC, abstractmethod


class SessionStorePort(ABC):
    @abstractmethod
    def check_duplicate(self, rca_id: str) -> bool: ...

    @abstractmethod
    def create_session(
        self,
        rca_id: str,
        alarm_name: str,
        idempotency_key: str,
        *,
        alarm_data: dict | None = None,
    ) -> bool: ...

    @abstractmethod
    def update_state(self, rca_id: str, state: str) -> None: ...

    @abstractmethod
    def mark_completed(self, rca_id: str, root_cause: str) -> None: ...

    @abstractmethod
    def mark_failed(self, rca_id: str, error_reason: str) -> None: ...

    @abstractmethod
    def mark_outdated(self, rca_id: str, reason: str) -> None: ...

    @abstractmethod
    def is_terminated(self, rca_id: str) -> bool: ...
