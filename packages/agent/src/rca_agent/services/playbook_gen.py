from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import (
    LLM_DEFAULT_TIMEOUT_SECONDS,
    PLAYBOOK_UPDATE_THRESHOLD,
)
from rca_agent.ports.dto.models import Playbook, PlaybookMatch, RcaReport, ScopingResult
from rca_agent.ports.interfaces.playbook_store import PlaybookStorePort
from rca_agent.prompts.playbook import (
    PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE,
    PLAYBOOK_USER_PROMPT_TEMPLATE,
)
from rca_agent.utils.timeout import call_with_timeout

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


class PlaybookOutput(BaseModel):
    failure_type: str
    symptom_pattern: str
    severity_criteria: str = ""
    verification_steps: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    escalation_criteria: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    related_metrics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class PlaybookUpdateOutput(BaseModel):
    needs_update: bool = True
    failure_type: str = ""
    symptom_pattern: str = ""
    severity_criteria: str = ""
    verification_steps: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    escalation_criteria: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    related_metrics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


_EMBED_FIELD_MAX = 80


def _truncate(text: str, max_len: int = _EMBED_FIELD_MAX) -> str:
    return text[:max_len].strip() if text else ""


def _build_embed_key(report: RcaReport, scoping_result: ScopingResult | None) -> str:
    metric_name = ""
    if scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
        metric_name = scoping_result.raw_alarm.trigger.metric_name
    parts = {
        "장애유형": _truncate(report.root_cause or "unknown"),
        "증상": _truncate(report.incident_summary),
        "메트릭": _truncate(metric_name),
    }
    return " | ".join(f"{k}: {v}" for k, v in parts.items() if v)


def _build_user_prompt(report: RcaReport) -> str:
    return PLAYBOOK_USER_PROMPT_TEMPLATE.format(
        failure_type="Inferred from root cause",
        root_cause=report.root_cause,
        severity=report.severity,
        evidence_highlights="\n".join(f"- {e}" for e in report.evidence_list[:5]) or "N/A",
        detection_method=report.detection_method or "N/A",
        mitigation_text=report.temporary_mitigation or "N/A",
        remediation_text=report.permanent_remediation or "N/A",
        action_items_text="\n".join(f"- {a}" for a in report.action_items) or "N/A",
    )


def _build_update_prompt(existing: PlaybookMatch, report: RcaReport) -> str:
    # Only failure_type/symptom_pattern/tags survive in the vector index metadata;
    # the remaining fields are unavailable at search time (ADR infra/0002).
    unavailable = "N/A (not in search index)"
    return PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE.format(
        existing_failure_type=existing.failure_type or "N/A",
        existing_symptom_pattern=existing.symptom_pattern or "N/A",
        existing_severity_criteria=unavailable,
        existing_verification_steps=unavailable,
        existing_temporary_mitigation=unavailable,
        existing_permanent_remediation=unavailable,
        existing_escalation_criteria=unavailable,
        existing_prevention_measures=unavailable,
        existing_related_metrics=unavailable,
        root_cause=report.root_cause,
        severity=report.severity,
        evidence_highlights="\n".join(f"  - {e}" for e in report.evidence_list[:5]) or "N/A",
        detection_method=report.detection_method or "N/A",
        mitigation_text=report.temporary_mitigation or "N/A",
        remediation_text=report.permanent_remediation or "N/A",
    )


def _invoke_agent(agent: Agent, prompt: str) -> PlaybookOutput:
    result = agent(prompt, structured_output_model=PlaybookOutput)
    return result.structured_output


def _invoke_update_agent(agent: Agent, prompt: str) -> PlaybookUpdateOutput:
    result = agent(prompt, structured_output_model=PlaybookUpdateOutput)
    return result.structured_output


def search_existing_playbooks(
    report: RcaReport,
    scoping_result: ScopingResult | None,
    *,
    playbook_store: PlaybookStorePort,
    threshold: float = PLAYBOOK_UPDATE_THRESHOLD,
) -> list[PlaybookMatch]:
    """Find playbooks close enough to update instead of creating a duplicate."""
    return playbook_store.search_similar(
        _build_embed_key(report, scoping_result),
        threshold=threshold,
    )


def _try_update_existing(
    hit: PlaybookMatch,
    report: RcaReport,
    update_agent: Agent,
    *,
    timeout_seconds: float = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Playbook | None:
    prompt = _build_update_prompt(hit, report)
    logger.info(
        "Checking update for playbook %s (similarity=%.2f)",
        hit.playbook_id,
        hit.similarity,
    )

    output: PlaybookUpdateOutput | None = None
    try:
        output = call_with_timeout(
            lambda: _invoke_update_agent(update_agent, prompt),
            timeout_seconds,
        )
    except Exception:
        logger.warning("Playbook update check failed for %s", hit.playbook_id)

    if output is None or not output.needs_update:
        if output and not output.needs_update:
            logger.info("Playbook %s is up-to-date, no update needed", hit.playbook_id)
        return None

    logger.info("Updating playbook %s with new RCA findings", hit.playbook_id)
    return Playbook(
        playbook_id=hit.playbook_id,
        failure_type=output.failure_type or hit.failure_type,
        symptom_pattern=output.symptom_pattern or hit.symptom_pattern,
        severity_criteria=output.severity_criteria,
        verification_steps=output.verification_steps,
        temporary_mitigation=output.temporary_mitigation,
        permanent_remediation=output.permanent_remediation,
        escalation_criteria=output.escalation_criteria,
        prevention_measures=output.prevention_measures,
        related_metrics=output.related_metrics,
        rca_id=report.rca_id,
        tags=output.tags or hit.tags,
    )


def run_playbook_generation(
    report: RcaReport,
    agent: Agent,
    *,
    playbook_store: PlaybookStorePort,
    scoping_result: ScopingResult | None = None,
    timeout_seconds: float = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Playbook:
    existing_hits = search_existing_playbooks(
        report,
        scoping_result,
        playbook_store=playbook_store,
    )
    deadline = time.monotonic() + max(0, timeout_seconds)

    for hit in existing_hits:
        remaining_seconds = max(0.0, deadline - time.monotonic())
        updated = _try_update_existing(
            hit,
            report,
            agent,
            timeout_seconds=remaining_seconds,
        )
        if updated is not None:
            return updated

    if existing_hits:
        logger.info("All %d existing playbooks are up-to-date", len(existing_hits))

    playbook_id = str(uuid.uuid4())
    user_prompt = _build_user_prompt(report)

    logger.info("Generating new playbook from RCA %s", report.rca_id)

    output: PlaybookOutput | None = None
    try:
        remaining_seconds = max(0.0, deadline - time.monotonic())
        output = call_with_timeout(
            lambda: _invoke_agent(agent, user_prompt),
            remaining_seconds,
        )
    except Exception:
        logger.warning("Playbook generation failed")

    if output is None:
        return Playbook(
            playbook_id=playbook_id,
            failure_type="unknown",
            symptom_pattern=report.incident_summary,
            rca_id=report.rca_id,
        )

    logger.info("Playbook generated: %s (type=%s)", playbook_id, output.failure_type)
    return Playbook(
        playbook_id=playbook_id,
        failure_type=output.failure_type,
        symptom_pattern=output.symptom_pattern,
        severity_criteria=output.severity_criteria,
        verification_steps=output.verification_steps,
        temporary_mitigation=output.temporary_mitigation,
        permanent_remediation=output.permanent_remediation,
        escalation_criteria=output.escalation_criteria,
        prevention_measures=output.prevention_measures,
        related_metrics=output.related_metrics,
        rca_id=report.rca_id,
        tags=output.tags,
    )
