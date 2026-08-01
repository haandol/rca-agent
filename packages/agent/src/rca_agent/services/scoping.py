from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import SCOPING_TIMEOUT_SECONDS
from rca_agent.ports.dto.models import (
    AlarmPayload,
    ConcurrentAlarm,
    MetricObservation,
    MetricTrend,
    ReportMatch,
    ScopingResult,
)
from rca_agent.ports.interfaces.report_store import ReportStorePort
from rca_agent.prompts.scoping import SCOPING_USER_PROMPT_TEMPLATE
from rca_agent.services.report_context import build_report_context
from rca_agent.utils.embed_key import build_embed_key
from rca_agent.utils.timeout import call_with_timeout

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


class MetricObservationOutput(BaseModel):
    """지표 하나에 대해 모델이 채워야 하는 관측.

    `datapoints` 를 요구하는 것이 이 모델의 요점이다. 모델이 조회한 시계열을 스스로
    두 숫자로 요약하게 두면 추세가 사라지고, 하류 단계는 사라진 것을 복원할 수 없다.
    """

    metric_name: str
    datapoints: list[float] = Field(default_factory=list)
    trend: MetricTrend = MetricTrend.UNKNOWN
    shape_note: str = ""
    window_start: str | None = None
    window_end: str | None = None
    unit: str = ""
    baseline: float | None = None


class ConcurrentAlarmOutput(BaseModel):
    alarm_name: str
    state: str = ""


class ScopingOutput(BaseModel):
    """Structured output model for the scoping agent."""

    alarm_summary: str
    anomaly_start_time: str | None = None
    blast_radius: str = "single"
    initial_severity: str = "medium"
    metric_observations: list[MetricObservationOutput] = Field(default_factory=list)
    concurrent_alarms: list[ConcurrentAlarmOutput] = Field(default_factory=list)


def _parse_window(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        logger.warning("Could not parse observation window: %s", value)
        return None


_MIN_TREND_DATAPOINTS = 2


def reconcile_trend(reported: MetricTrend, datapoints: list[float]) -> MetricTrend:
    """모델이 읽은 추세를 시퀀스와 대조한다.

    추세 자체는 모델이 판정한다. 서버가 규칙으로 도출하면 규칙이 모르는 형태(계단형,
    톱니형, 주기적 진동)를 가장 가까운 항목으로 뭉개고, 그 판정을 모델이 반박할 수
    없다. 추세는 파괴적 액션이나 해결 판정과 달리 되돌릴 수 없는 결정이 아니므로 서버가
    권위를 가질 이유가 없다.

    서버가 막는 것은 하나다 — **근거 없이 형태를 단정하는 것.** 데이터포인트가 두 개
    미만이면 어떤 추세도 관측되지 않았으므로 미확정으로 되돌린다. 라이브의 오독은 모델이
    추세를 판정해서가 아니라 판정할 시퀀스가 전달되지 않아 생겼다.
    """
    if len(datapoints) < _MIN_TREND_DATAPOINTS:
        return MetricTrend.UNKNOWN
    return reported


def _to_observation(item: MetricObservationOutput) -> MetricObservation:
    return MetricObservation(
        metric_name=item.metric_name,
        datapoints=item.datapoints,
        trend=reconcile_trend(item.trend, item.datapoints),
        shape_note=item.shape_note,
        window_start=_parse_window(item.window_start),
        window_end=_parse_window(item.window_end),
        unit=item.unit,
        baseline=item.baseline,
    )


def _build_user_prompt(alarm: AlarmPayload, reports: list[ReportMatch]) -> str:
    trigger = alarm.trigger
    return SCOPING_USER_PROMPT_TEMPLATE.format(
        alarm_name=alarm.alarm_name,
        state_reason=alarm.new_state_reason,
        state_change_time=alarm.state_change_time or "N/A",
        region=alarm.region,
        namespace=trigger.namespace if trigger else "N/A",
        metric_name=trigger.metric_name if trigger else "N/A",
        dimensions=json.dumps(trigger.dimensions, ensure_ascii=False) if trigger else "{}",
        statistic=trigger.statistic if trigger else "N/A",
        period=trigger.period if trigger else 300,
        threshold=trigger.threshold if trigger else "N/A",
        comparison_operator=trigger.comparison_operator if trigger else "N/A",
        report_context=build_report_context(reports),
    )


def build_report_query(alarm: AlarmPayload) -> str:
    """Build the structured embedding query shared with the report index writer."""
    return build_embed_key(
        failure_type=alarm.alarm_name,
        symptom=alarm.new_state_reason,
        metric_name=alarm.trigger.metric_name if alarm.trigger else "",
    )


def _invoke_scoping_agent(
    agent: Agent,
    user_prompt: str,
) -> ScopingOutput:
    result = agent(user_prompt, structured_output_model=ScopingOutput)
    return result.structured_output


def run_scoping(
    alarm: AlarmPayload,
    agent: Agent,
    *,
    report_store: ReportStorePort,
    timeout_seconds: int = SCOPING_TIMEOUT_SECONDS,
) -> ScopingResult:
    reports = report_store.search_similar(build_report_query(alarm))
    user_prompt = _build_user_prompt(alarm, reports)

    logger.info("Running scoping agent for alarm: %s (timeout=%ds)", alarm.alarm_name, timeout_seconds)

    output: ScopingOutput | None = None
    try:
        output = call_with_timeout(
            lambda: _invoke_scoping_agent(agent, user_prompt),
            timeout_seconds,
        )
    except TimeoutError:
        logger.warning("Scoping agent timed out after %ds, using alarm payload as fallback", timeout_seconds)
    except Exception:
        logger.exception("Scoping agent failed")

    if output is None:
        return ScopingResult(
            alarm_summary=f"[Timeout] {alarm.alarm_name}: {alarm.new_state_reason}",
            blast_radius="single",
            initial_severity="medium",
            similar_reports=reports,
            raw_alarm=alarm,
        )

    logger.info("Scoping complete: severity=%s, blast_radius=%s", output.initial_severity, output.blast_radius)

    anomaly_time = _parse_window(output.anomaly_start_time)

    return ScopingResult(
        alarm_summary=output.alarm_summary,
        anomaly_start_time=anomaly_time,
        blast_radius=output.blast_radius,
        initial_severity=output.initial_severity,
        metric_observations=[_to_observation(item) for item in output.metric_observations],
        concurrent_alarms=[
            ConcurrentAlarm(alarm_name=item.alarm_name, state=item.state) for item in output.concurrent_alarms
        ],
        similar_reports=reports,
        raw_alarm=alarm,
    )
