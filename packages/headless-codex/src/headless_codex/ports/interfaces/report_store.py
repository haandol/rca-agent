from __future__ import annotations

from abc import ABC, abstractmethod

from headless_codex.ports.dto.models import AlarmContext


class ReportStorePort(ABC):
    @abstractmethod
    def save_report(
        self,
        rca_id: str,
        report_markdown: str,
        *,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> str: ...

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
        confirmed: bool = False,
        alarm_context: AlarmContext | None = None,
    ) -> None: ...
