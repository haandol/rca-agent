from __future__ import annotations

from abc import ABC, abstractmethod


class ReportStorePort(ABC):
    @abstractmethod
    def save_report(self, rca_id: str, report_markdown: str) -> str: ...

    @abstractmethod
    def send_notification(
        self,
        rca_id: str,
        alarm_name: str,
        root_cause: str,
        report_s3_key: str,
        elapsed_seconds: int,
        *,
        playbook: dict | None = None,
    ) -> None: ...
