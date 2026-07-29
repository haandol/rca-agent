from __future__ import annotations

import json
import logging
from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import SCOPING_TIMEOUT_SECONDS
from rca_agent.ports.dto.models import AlarmPayload, ReportMatch, ScopingResult
from rca_agent.ports.interfaces.report_store import ReportStorePort
from rca_agent.prompts.scoping import SCOPING_USER_PROMPT_TEMPLATE
from rca_agent.services.report_context import build_report_context
from rca_agent.utils.embed_key import build_embed_key
from rca_agent.utils.timeout import call_with_timeout

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


class ScopingOutput(BaseModel):
    """Structured output model for the scoping agent."""

    alarm_summary: str
    anomaly_start_time: str | None = None
    blast_radius: str = "single"
    initial_severity: str = "medium"
    metric_snapshot: dict[str, dict] = Field(default_factory=dict)


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

    anomaly_time = None
    if output.anomaly_start_time:
        from datetime import datetime

        try:
            anomaly_time = datetime.fromisoformat(output.anomaly_start_time).replace(tzinfo=UTC)
        except ValueError:
            logger.warning("Could not parse anomaly_start_time: %s", output.anomaly_start_time)

    return ScopingResult(
        alarm_summary=output.alarm_summary,
        anomaly_start_time=anomaly_time,
        blast_radius=output.blast_radius,
        initial_severity=output.initial_severity,
        metric_snapshot=output.metric_snapshot,
        similar_reports=reports,
        raw_alarm=alarm,
    )
