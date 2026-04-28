from __future__ import annotations

from abc import ABC, abstractmethod

from rca_agent.ports.dto.models import NotificationMessage


class NotificationPort(ABC):
    @abstractmethod
    def send(self, notification: NotificationMessage) -> bool: ...

    @abstractmethod
    def generate_report_url(self, report_s3_key: str) -> str: ...
