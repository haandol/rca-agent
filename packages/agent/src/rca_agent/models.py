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


class HypothesisCategory(StrEnum):
    DEPLOYMENT = "DEPLOYMENT"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    TRAFFIC = "TRAFFIC"
    DEPENDENCY = "DEPENDENCY"
    CONFIGURATION = "CONFIGURATION"


class HypothesisStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"


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

    @classmethod
    def from_cloudwatch_sns(cls, raw: dict) -> AlarmPayload:
        """Parse a CloudWatch alarm SNS notification into AlarmPayload."""
        trigger_raw = raw.get("Trigger") or {}
        dimensions = {d["name"]: d["value"] for d in trigger_raw.get("Dimensions", [])}

        trigger = None
        if trigger_raw.get("MetricName"):
            trigger = AlarmTrigger(
                metric_name=trigger_raw["MetricName"],
                namespace=trigger_raw.get("Namespace", ""),
                dimensions=dimensions,
                statistic=trigger_raw.get("Statistic", "Average"),
                period=trigger_raw.get("Period", 300),
                threshold=trigger_raw.get("Threshold"),
                comparison_operator=trigger_raw.get("ComparisonOperator"),
            )

        alarm_arn = raw.get("AlarmArn") or None
        region = "us-east-1"
        if alarm_arn:
            arn_parts = alarm_arn.split(":")
            if len(arn_parts) >= 4:
                region = arn_parts[3]

        return cls(
            alarm_name=raw.get("AlarmName", ""),
            alarm_arn=alarm_arn,
            new_state=raw.get("NewStateValue", "ALARM"),
            new_state_reason=raw.get("NewStateReason", ""),
            state_change_time=raw.get("StateChangeTime"),
            trigger=trigger,
            region=region,
        )


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


class Hypothesis(BaseModel):
    hypothesis_id: str = ""
    description: str
    category: HypothesisCategory
    confidence_score: float = Field(ge=0.0, le=1.0)
    required_evidence: list[str] = Field(default_factory=list)
    referenced_playbook_id: str | None = None
    status: HypothesisStatus = HypothesisStatus.PENDING
    tree_id: str = ""
    parent_id: str | None = None
    depth: int = 0


class HypothesisGenerationResult(BaseModel):
    tree_id: str
    hypotheses: list[Hypothesis]
    scoping_result: ScopingResult
