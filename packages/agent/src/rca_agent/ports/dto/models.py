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
    CLOSED = "CLOSED"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"


class FaultType(StrEnum):
    DB_CONNECTION_LEAK = "DB_CONNECTION_LEAK"
    HIGH_CPU = "HIGH_CPU"
    HIGH_MEMORY = "HIGH_MEMORY"
    SLOW_QUERY = "SLOW_QUERY"
    UNSUPPORTED = "UNSUPPORTED"


class PlaybookVerificationStatus(StrEnum):
    """플레이북 절차가 실행으로 검증되었는지.

    실행되지 않은 플레이북은 초안이다. 실행과 회고를 거친 뒤에야 검증된 절차가 되며,
    그 전이는 분석이 아니라 실행 주체가 수행한다. 분석은 DRAFT만 쓸 수 있다.

    검증 상태는 현재 실행 절차 내용에 종속된다. 절차가 같으면 기존 상태를 보존하고,
    실행 절차가 추가·교정되면 새 내용이 다시 실행될 때까지 초안으로 돌아간다.
    """

    DRAFT = "DRAFT"
    VERIFIED = "VERIFIED"


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
    """Playbook hit from the vector index.

    Only fields kept in the S3 Vectors metadata are available here. ``rca_id``
    identifies the RCA that produced the playbook and is the key used to load
    the detail fields, which the index does not carry.
    """

    playbook_id: str
    similarity: float
    failure_type: str = ""
    symptom_pattern: str = ""
    tags: list[str] = Field(default_factory=list)
    rca_id: str = ""
    # A hit has to say whether its procedure was proven by an execution without a
    # second lookup. Absent on records written before the field existed, and an
    # unproven procedure must not read as verified, so the default is a draft.
    verification_status: PlaybookVerificationStatus = PlaybookVerificationStatus.DRAFT


class ReportMatch(BaseModel):
    rca_id: str
    similarity: float
    incident_summary: str = ""
    root_cause: str = ""
    hypothesis_path: str = ""
    confirmed: bool = False


class MetricTrend(StrEnum):
    RISING = "rising"
    FALLING = "falling"
    FLAT = "flat"
    SPIKE = "spike"
    UNKNOWN = "unknown"


class MetricObservation(BaseModel):
    """한 지표의 관측 결과.

    현재 값과 기준선 두 숫자만 남기면 하류 단계가 "지금 높다"만 알고 "계속 오르는
    중인가, 한 번 튀었다 내려왔나"를 구별할 수 없다. 그 구별이 누수와 일시적 부하를
    가르는 근거이므로 시퀀스를 보유한다.

    `trend` 는 모델이 읽은 요약이고 `datapoints` 가 근거다. 어휘에 담기지 않는 형태는
    `shape_note` 로 서술한다 — 다섯 항목으로 뭉개면 처음 보는 패턴이 사라진다.
    """

    metric_name: str
    datapoints: list[float] = Field(default_factory=list)
    trend: MetricTrend = MetricTrend.UNKNOWN
    shape_note: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    unit: str = ""
    baseline: float | None = None


class ConcurrentAlarm(BaseModel):
    """스코핑이 확인한 동시 발생 알람.

    확인만 지시하고 담을 곳을 두지 않으면 확인은 수행되어도 그 사실이 검증 단계에
    도달하지 못하고, 근거 없는 반대 서술이 성립한다.
    """

    alarm_name: str
    state: str = ""


class ScopingResult(BaseModel):
    alarm_summary: str
    anomaly_start_time: datetime | None = None
    blast_radius: str = "single"
    initial_severity: str = "medium"
    metric_observations: list[MetricObservation] = Field(default_factory=list)
    concurrent_alarms: list[ConcurrentAlarm] = Field(default_factory=list)
    similar_reports: list[ReportMatch] = Field(default_factory=list)
    raw_alarm: AlarmPayload | None = None


class Hypothesis(BaseModel):
    hypothesis_id: str = ""
    title: str = ""  # 짧은 한 줄 제목 (~60자). 없으면 description 첫 줄이 fallback.
    description: str
    category: HypothesisCategory
    confidence_score: float = Field(ge=0.0, le=1.0)
    required_evidence: list[str] = Field(default_factory=list)
    referenced_playbook_id: str | None = None
    fault_type: FaultType = FaultType.UNSUPPORTED
    validated_fault_type: FaultType = FaultType.UNSUPPORTED
    judgment_reasoning: str = ""
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
    validated_fault_type: FaultType = FaultType.UNSUPPORTED


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
    severity: str = "medium"
    impact_summary: str = ""
    detection_method: str = ""
    root_cause: str
    root_cause_confirmed: bool = True
    confidence_score: float = Field(ge=0.0, le=1.0)
    hypothesis_path: list[str] = Field(default_factory=list)
    five_whys: list[str] = Field(default_factory=list)
    evidence_list: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    action_items: list[str] = Field(default_factory=list)
    lessons_learned: str = ""
    timeline: list[str] = Field(default_factory=list)
    rejected_hypotheses: list[str] = Field(default_factory=list)


class ExecutionStep(BaseModel):
    """실행 에이전트가 수행할 한 단계.

    ``action`` 은 자연어다. 명령 문자열을 고정하지 않는 이유는 대상 리소스 식별자와
    리전이 실행 시점의 알람 컨텍스트에서 결정되기 때문이며, 절차에 박아 넣으면 같은
    유형의 다른 리소스 장애에 재사용할 수 없다.

    ``step_id`` 는 안정적이어야 한다. 실행 증거가 어느 단계에서 실패했는지 지목하고
    회고가 그 단계를 교정하기 때문이다.
    """

    step_id: str
    intent: str = ""
    action: str = ""
    success_criteria: str = ""


class Playbook(BaseModel):
    playbook_id: str
    failure_type: str
    symptom_pattern: str
    severity_criteria: str = ""
    verification_steps: list[str] = Field(default_factory=list)
    execution_steps: list[ExecutionStep] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    escalation_criteria: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    related_metrics: list[str] = Field(default_factory=list)
    rca_id: str = ""
    tags: list[str] = Field(default_factory=list)
    # 분석은 이 값을 바꾸지 않는다. 실행되지 않은 절차는 검증되지 않았다.
    verification_status: PlaybookVerificationStatus = PlaybookVerificationStatus.DRAFT


class AlarmContext(BaseModel):
    """Alarm context carried in the RCA notification.

    실행 에이전트가 절차의 자연어 서술을 실제 리소스에 매핑할 때 쓰는 컨텍스트이며,
    알림 수신자에게는 어떤 알람의 분석인지를 알려준다.
    """

    alarm_name: str = ""
    region: str = "us-east-1"
    namespace: str = ""
    metric_name: str = ""
    dimensions: dict[str, str] = Field(default_factory=dict)
    statistic: str = "Average"
    period: int = 300
    threshold: float | None = None
    comparison_operator: str | None = None


class NotificationMessage(BaseModel):
    """분석 완료 알림.

    수신자는 사람과 대시보드뿐이다. 어떤 기계 소비자도 이 알림을 받아 쓰기 작업을
    시작하지 않으며, 실행은 사용자가 승인 요청을 발행할 때만 시작된다. 그래서 payload
    는 승인 판단에 필요한 정보만 담고 실행 절차 자체는 담지 않는다 — 실행 주체는
    저장된 리포트를 직접 읽는다.
    """

    rca_id: str
    publication_id: str = ""
    root_cause_summary: str
    severity: str
    report_s3_key: str = ""
    dashboard_url: str = ""
    elapsed_seconds: int = 0
    confirmed: bool = True
    playbook: dict | None = None
    root_cause: str = ""
    selected_hypothesis_id: str = ""
    alarm_context: AlarmContext | None = None
    event_type: str = "rca_complete"


class CompletionHandoff(BaseModel):
    rca_id: str
    state: RcaSessionState
    notification_status: str = ""
    notification: NotificationMessage | None = None


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
    claim_token: str = ""
    receive_count: int = 0
    message_id: str = ""
