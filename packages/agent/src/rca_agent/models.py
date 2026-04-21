from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RcaSessionState(StrEnum):
    ALARM_RECEIVED = "ALARM_RECEIVED"
    SCOPING = "SCOPING"
    HYPOTHESIS_GENERATION = "HYPOTHESIS_GENERATION"
    HYPOTHESIS_PRIORITIZATION = "HYPOTHESIS_PRIORITIZATION"
    EVIDENCE_COLLECTION = "EVIDENCE_COLLECTION"
    HYPOTHESIS_VALIDATION = "HYPOTHESIS_VALIDATION"
    REPORT_GENERATION = "REPORT_GENERATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AlarmTrigger(BaseModel):
    metric_name: str
    namespace: str
    dimensions: dict[str, str] = Field(default_factory=dict)
    statistic: str = "Average"
    period: int = 300
    threshold: float | None = None
    comparison_operator: str | None = None


class AlarmPayload(BaseModel):
    alarm_name: str
    alarm_arn: str | None = None
    new_state: str = "ALARM"
    new_state_reason: str = ""
    state_change_time: datetime | None = None
    trigger: AlarmTrigger | None = None
    region: str = "us-east-1"

    @property
    def resource_id(self) -> str:
        if self.trigger and self.trigger.dimensions:
            return next(iter(self.trigger.dimensions.values()), self.alarm_name)
        return self.alarm_name

    @property
    def service_name(self) -> str:
        if self.trigger:
            return self.trigger.namespace
        return "Unknown"


class PlaybookMatch(BaseModel):
    playbook_id: str
    title: str
    similarity: float
    root_cause_summary: str = ""


class ScopingResult(BaseModel):
    alarm_summary: str
    anomaly_start_time: datetime | None = None
    blast_radius: str = "single"
    initial_severity: str = "medium"
    metric_snapshot: dict = Field(default_factory=dict)
    similar_playbooks: list[PlaybookMatch] = Field(default_factory=list)
    raw_alarm: AlarmPayload | None = None
