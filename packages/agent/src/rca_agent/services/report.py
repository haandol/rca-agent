from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import LLM_DEFAULT_TIMEOUT_SECONDS, S3_REPORT_BUCKET
from rca_agent.ports.dto.models import (
    Hypothesis,
    RcaReport,
    ScopingResult,
)
from rca_agent.prompts.report import REPORT_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

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


def _build_user_prompt(
    scoping: ScopingResult,
    best_hypothesis: Hypothesis | None,
    confirmed: bool,
    hypothesis_path: list[str],
    evidence_texts: list[str],
    rejected_descriptions: list[str],
    timeline: list[str],
) -> str:
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
        alarm_name=alarm_name or "N/A",
        metric_name=metric_name or "N/A",
        confirmed="Yes" if confirmed else "No (most likely candidate)",
        root_cause_description=root_cause_desc,
        confidence=f"{confidence:.2f}",
        hypothesis_path="\n".join(f"- {p}" for p in hypothesis_path) or "N/A",
        evidence_text="\n".join(f"- {e}" for e in evidence_texts) or "No evidence collected.",
        rejected_text="\n".join(f"- {r}" for r in rejected_descriptions) or "None",
        timeline_text="\n".join(f"- {t}" for t in timeline) or "N/A",
    )


def _invoke_agent(agent: Agent, prompt: str) -> ReportOutput:
    result = agent(prompt, structured_output_model=ReportOutput)
    return result.structured_output


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
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke_agent, agent, user_prompt)
        try:
            output = future.result(timeout=timeout_seconds)
        except (FuturesTimeoutError, Exception):
            logger.warning("Report generation failed, building minimal report")

    if output is None:
        return RcaReport(
            rca_id=rca_id,
            incident_summary=scoping_result.alarm_summary,
            severity=scoping_result.initial_severity,
            root_cause=best_hypothesis.description if best_hypothesis else "Unknown",
            root_cause_confirmed=confirmed,
            confidence_score=best_hypothesis.confidence_score if best_hypothesis else 0.0,
            hypothesis_path=hypothesis_path,
            evidence_list=evidence_texts,
            rejected_hypotheses=rejected_descriptions,
            timeline=timeline,
        )

    logger.info("RCA report generated (rca_id=%s)", rca_id)
    return RcaReport(
        rca_id=rca_id,
        incident_summary=output.incident_summary,
        severity=output.severity,
        impact_summary=output.impact_summary,
        detection_method=output.detection_method,
        root_cause=output.root_cause,
        root_cause_confirmed=confirmed,
        confidence_score=best_hypothesis.confidence_score if best_hypothesis else 0.0,
        hypothesis_path=hypothesis_path,
        evidence_list=evidence_texts,
        temporary_mitigation=output.temporary_mitigation,
        permanent_remediation=output.permanent_remediation,
        action_items=output.action_items,
        lessons_learned=output.lessons_learned,
        timeline=output.timeline,
        rejected_hypotheses=rejected_descriptions,
    )


def save_report_to_s3(report: RcaReport, *, s3_client=None) -> str:
    if not S3_REPORT_BUCKET or s3_client is None:
        logger.info("S3 report bucket not configured, skipping upload")
        return ""

    key = f"reports/{report.rca_id}.md"
    body = _render_markdown(report)

    try:
        s3_client.put_object(Bucket=S3_REPORT_BUCKET, Key=key, Body=body, ContentType="text/markdown")
        logger.info("Report saved to s3://%s/%s", S3_REPORT_BUCKET, key)
        return key
    except Exception:
        logger.exception("Failed to save report to S3")
        return ""


def _render_markdown(report: RcaReport) -> str:
    confirmed_label = "Confirmed" if report.root_cause_confirmed else "Unconfirmed (most likely candidate)"
    lines = [
        f"# RCA Report: {report.rca_id}",
        "",
        "## Incident Summary",
        report.incident_summary,
        "",
        f"- **Severity**: {report.severity}",
    ]
    if report.detection_method:
        lines.append(f"- **Detection**: {report.detection_method}")
    lines.append("")

    if report.impact_summary:
        lines.extend(["## Impact Assessment", report.impact_summary, ""])

    lines.extend(
        [
            "## Root Cause",
            f"**Status**: {confirmed_label}",
            f"**Confidence**: {report.confidence_score:.2f}",
            "",
            report.root_cause,
            "",
        ]
    )

    if report.hypothesis_path:
        lines.append("## Hypothesis Path")
        for p in report.hypothesis_path:
            lines.append(f"- {p}")
        lines.append("")

    if report.evidence_list:
        lines.append("## Evidence")
        for e in report.evidence_list:
            lines.append(f"- {e}")
        lines.append("")

    if report.timeline:
        lines.append("## Timeline")
        for t in report.timeline:
            lines.append(f"- {t}")
        lines.append("")

    if report.temporary_mitigation:
        lines.extend(["## Temporary Mitigation", report.temporary_mitigation, ""])

    if report.permanent_remediation:
        lines.extend(["## Permanent Remediation", report.permanent_remediation, ""])

    if report.action_items:
        lines.append("## Action Items")
        for item in report.action_items:
            lines.append(f"- {item}")
        lines.append("")

    if report.lessons_learned:
        lines.extend(["## Lessons Learned", report.lessons_learned, ""])

    if report.rejected_hypotheses:
        lines.append("## Rejected Hypotheses")
        for r in report.rejected_hypotheses:
            lines.append(f"- {r}")
        lines.append("")

    return "\n".join(lines)
