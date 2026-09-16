from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import LLM_DEFAULT_TIMEOUT_SECONDS
from rca_agent.ports.dto.models import (
    Hypothesis,
    RcaReport,
    ScopingResult,
)
from rca_agent.prompts.report import REPORT_USER_PROMPT_TEMPLATE
from rca_agent.services.observation_context import render_alarm_description, render_critical_facts
from rca_agent.utils.agent_invocation import invoke_agent

if TYPE_CHECKING:
    from strands import Agent

from rca_agent.utils.exception_logging import safe_exception_info

logger = logging.getLogger(__name__)


class ReportOutput(BaseModel):
    incident_summary: str
    severity: str = "medium"
    impact_summary: str = ""
    detection_method: str = ""
    root_cause: str
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    action_items: list[str] = Field(default_factory=list)
    lessons_learned: str = ""
    timeline: list[str] = Field(default_factory=list)
    five_whys: list[str] = Field(default_factory=list)


def _build_user_prompt(
    scoping: ScopingResult,
    best_hypothesis: Hypothesis | None,
    confirmed: bool,
    hypothesis_path: list[str],
    evidence_texts: list[str],
    rejected_descriptions: list[str],
    timeline: list[str],
) -> str:
    """Keep discovery metadata distinct from findings in the report model's context."""
    root_cause_desc = best_hypothesis.description if best_hypothesis else "Unknown"
    confidence = best_hypothesis.confidence_score if best_hypothesis else 0.0

    alarm_name = ""
    metric_name = ""
    if scoping.raw_alarm:
        alarm_name = scoping.raw_alarm.alarm_name
        if scoping.raw_alarm.trigger:
            metric_name = scoping.raw_alarm.trigger.metric_name

    return REPORT_USER_PROMPT_TEMPLATE.format(
        incident_summary=scoping.alarm_summary,
        alarm_description=render_alarm_description(scoping.raw_alarm),
        alarm_name=alarm_name or "N/A",
        metric_name=metric_name or "N/A",
        confirmed="Yes" if confirmed else "No (most likely candidate)",
        root_cause_description=root_cause_desc,
        confidence=f"{confidence:.2f}",
        hypothesis_path="\n".join(f"- {p}" for p in hypothesis_path) or "N/A",
        evidence_text="\n".join(f"- {e}" for e in evidence_texts) or "No evidence collected.",
        rejected_text="\n".join(f"- {r}" for r in rejected_descriptions) or "None",
        timeline_text="\n".join(f"- {t}" for t in timeline) or "N/A",
    ) + render_critical_facts(scoping)


def _selected_hypothesis_fields(best_hypothesis: Hypothesis | None, confirmed: bool) -> dict:
    """Use the same server-owned cause metadata for generated and fallback reports."""
    return {
        "root_cause": best_hypothesis.description if best_hypothesis else "Unknown",
        "root_cause_confirmed": confirmed,
        "confidence_score": best_hypothesis.confidence_score if best_hypothesis else 0.0,
        "selected_hypothesis_id": best_hypothesis.hypothesis_id if best_hypothesis else "",
        "selected_hypothesis_title": (
            best_hypothesis.title or best_hypothesis.description.splitlines()[0] if best_hypothesis else ""
        ),
    }


def run_report_generation(
    scoping_result: ScopingResult,
    best_hypothesis: Hypothesis | None,
    confirmed: bool,
    hypothesis_path: list[str],
    evidence_texts: list[str],
    rejected_descriptions: list[str],
    timeline: list[str],
    agent: Agent,
    *,
    timeout_seconds: int = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> RcaReport:
    """Preserve the supplied discovery description even when report generation falls back."""
    rca_id = str(uuid.uuid4())
    user_prompt = _build_user_prompt(
        scoping_result,
        best_hypothesis,
        confirmed,
        hypothesis_path,
        evidence_texts,
        rejected_descriptions,
        timeline,
    )

    logger.info("Generating RCA report (rca_id=%s)", rca_id)

    output: ReportOutput | None = None
    try:
        output = invoke_agent(agent, user_prompt, ReportOutput, timeout_seconds)
    except Exception as exc:
        logger.warning(
            "Report generation failed; exception_type=%s; building minimal report",
            type(exc).__name__,
            exc_info=safe_exception_info(exc),
        )

    if output is None:
        return RcaReport(
            rca_id=rca_id,
            incident_observations=scoping_result.incident_observations.model_copy(deep=True),
            incident_summary=scoping_result.alarm_summary,
            alarm_description=scoping_result.raw_alarm.alarm_description if scoping_result.raw_alarm else None,
            severity=scoping_result.initial_severity,
            **_selected_hypothesis_fields(best_hypothesis, confirmed),
            hypothesis_path=hypothesis_path,
            evidence_list=evidence_texts,
            rejected_hypotheses=rejected_descriptions,
            timeline=timeline,
        )

    logger.info("RCA report generated (rca_id=%s)", rca_id)
    return RcaReport(
        rca_id=rca_id,
        incident_observations=scoping_result.incident_observations.model_copy(deep=True),
        incident_summary=output.incident_summary,
        alarm_description=scoping_result.raw_alarm.alarm_description if scoping_result.raw_alarm else None,
        severity=output.severity,
        impact_summary=output.impact_summary,
        detection_method=output.detection_method,
        **_selected_hypothesis_fields(best_hypothesis, confirmed),
        hypothesis_path=hypothesis_path,
        evidence_list=evidence_texts,
        temporary_mitigation=output.temporary_mitigation,
        permanent_remediation=output.permanent_remediation,
        action_items=output.action_items,
        lessons_learned=output.lessons_learned,
        timeline=output.timeline,
        five_whys=output.five_whys,
        rejected_hypotheses=rejected_descriptions,
    )
