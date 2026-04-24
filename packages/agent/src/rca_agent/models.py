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
    OUTDATED = "OUTDATED"
    CANCELLED = "CANCELLED"


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
            alarm_name=raw.get("AlarmName") or "UnknownAlarm",
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


class ValidationPlan(BaseModel):
    tools: list[str] = Field(default_factory=list)
    estimated_seconds: int = 60


class PrioritizedHypothesis(BaseModel):
    hypothesis_id: str
    priority_rank: int
    validation_plan: ValidationPlan = Field(default_factory=ValidationPlan)
    parallel_group: int = 0


class PrioritizationResult(BaseModel):
    tree_id: str
    prioritized: list[PrioritizedHypothesis]


class ValidationJudgment(BaseModel):
    hypothesis_id: str
    status: HypothesisStatus
    confidence_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""
    evidence_summary: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    tree_id: str
    judgments: list[ValidationJudgment]
    all_rejected: bool = False


class BranchingResult(BaseModel):
    tree_id: str
    parent_id: str
    children: list[Hypothesis]


class TerminationReason(StrEnum):
    CONFIRMED = "CONFIRMED"
    TIME_BUDGET = "TIME_BUDGET"
    TOKEN_BUDGET = "TOKEN_BUDGET"
    MAX_DEPTH = "MAX_DEPTH"
    MAX_LOOPS = "MAX_LOOPS"
    ALL_REJECTED = "ALL_REJECTED"


class TerminationDecision(BaseModel):
    should_terminate: bool
    reason: TerminationReason | None = None
    best_hypothesis: Hypothesis | None = None


class RcaReport(BaseModel):
    rca_id: str
    incident_summary: str
    root_cause: str
    root_cause_confirmed: bool = True
    confidence_score: float = Field(ge=0.0, le=1.0)
    hypothesis_path: list[str] = Field(default_factory=list)
    evidence_list: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    timeline: list[str] = Field(default_factory=list)
    rejected_hypotheses: list[str] = Field(default_factory=list)


class Playbook(BaseModel):
    playbook_id: str
    failure_type: str
    symptom_pattern: str
    verification_steps: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    rca_id: str = ""
    tags: list[str] = Field(default_factory=list)


class RemediationAction(BaseModel):
    action_type: str
    description: str
    target: str = ""
    parameters: dict = Field(default_factory=dict)
    executed: bool = False
    success: bool = False
    error: str = ""


class RemediationResult(BaseModel):
    rca_id: str
    actions_taken: list[RemediationAction] = Field(default_factory=list)
    overall_success: bool = False
    summary: str = ""


class VerificationResult(BaseModel):
    rca_id: str
    metrics_normalized: bool = False
    verification_summary: str = ""
    remaining_issues: list[str] = Field(default_factory=list)


class NotificationMessage(BaseModel):
    rca_id: str
    root_cause_summary: str
    severity: str
    report_s3_key: str = ""
    dashboard_url: str = ""
    elapsed_seconds: int = 0
    confirmed: bool = True
    playbook: dict | None = None


class RcaSession(BaseModel):
    rca_id: str
    idempotency_key: str
    state: RcaSessionState = RcaSessionState.ALARM_RECEIVED
    alarm_name: str = ""
    alarm_arn: str = ""
    engine: str = "strands"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    ttl: int = 0
