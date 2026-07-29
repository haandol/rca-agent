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
from rca_agent.utils.embed_key import build_embed_key
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


def _build_embed_key(report: RcaReport, scoping_result: ScopingResult | None) -> str:
    metric_name = ""
    if scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
        metric_name = scoping_result.raw_alarm.trigger.metric_name
    return build_embed_key(
        failure_type=report.root_cause or "unknown",
        symptom=report.incident_summary,
        metric_name=metric_name,
    )


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


def _build_update_prompt(existing: Playbook, report: RcaReport) -> str:
    return PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE.format(
        existing_failure_type=existing.failure_type or "N/A",
        existing_symptom_pattern=existing.symptom_pattern or "N/A",
        existing_severity_criteria=existing.severity_criteria or "N/A",
        existing_verification_steps="\n".join(f"  - {s}" for s in existing.verification_steps) or "N/A",
        existing_temporary_mitigation=existing.temporary_mitigation or "N/A",
        existing_permanent_remediation=existing.permanent_remediation or "N/A",
        existing_escalation_criteria=existing.escalation_criteria or "N/A",
        existing_prevention_measures="\n".join(f"  - {m}" for m in existing.prevention_measures) or "N/A",
        existing_related_metrics="\n".join(f"  - {m}" for m in existing.related_metrics) or "N/A",
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
) -> list[PlaybookMatch]:
    """Find playbooks close enough to update instead of creating a duplicate.

    Uses a stricter threshold than plain retrieval: merging into the wrong
    playbook is worse than writing a new one.
    """
    return playbook_store.search_similar(
        _build_embed_key(report, scoping_result),
        threshold=PLAYBOOK_UPDATE_THRESHOLD,
    )


def _try_update_existing(
    existing: Playbook,
    report: RcaReport,
    update_agent: Agent,
    *,
    similarity: float = 0.0,
    timeout_seconds: float = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Playbook | None:
    prompt = _build_update_prompt(existing, report)
    logger.info(
        "Checking update for playbook %s (similarity=%.2f)",
        existing.playbook_id,
        similarity,
    )

    output: PlaybookUpdateOutput | None = None
    try:
        output = call_with_timeout(
            lambda: _invoke_update_agent(update_agent, prompt),
            timeout_seconds,
        )
    except Exception:
        logger.warning("Playbook update check failed for %s", existing.playbook_id)

    if output is None or not output.needs_update:
        if output and not output.needs_update:
            logger.info("Playbook %s is up-to-date, no update needed", existing.playbook_id)
        return None

    logger.info("Updating playbook %s with new RCA findings", existing.playbook_id)
    # An empty field means the LLM had nothing to add, not that the step was
    # dropped — keep the recorded value so a merge never loses past procedure.
    return Playbook(
        playbook_id=existing.playbook_id,
        failure_type=output.failure_type or existing.failure_type,
        symptom_pattern=output.symptom_pattern or existing.symptom_pattern,
        severity_criteria=output.severity_criteria or existing.severity_criteria,
        verification_steps=output.verification_steps or existing.verification_steps,
        temporary_mitigation=output.temporary_mitigation or existing.temporary_mitigation,
        permanent_remediation=output.permanent_remediation or existing.permanent_remediation,
        escalation_criteria=output.escalation_criteria or existing.escalation_criteria,
        prevention_measures=output.prevention_measures or existing.prevention_measures,
        related_metrics=output.related_metrics or existing.related_metrics,
        rca_id=report.rca_id,
        tags=output.tags or existing.tags,
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

    merge_candidates = 0
    for hit in existing_hits:
        # Merging without the recorded procedure would overwrite it under the same
        # id, so a hit we cannot load is left alone rather than half-updated.
        existing = playbook_store.load_detail(hit)
        if existing is None:
            logger.info(
                "Skipping playbook %s — detail unavailable, cannot merge safely",
                hit.playbook_id,
            )
            continue
        merge_candidates += 1
        remaining_seconds = max(0.0, deadline - time.monotonic())
        updated = _try_update_existing(
            existing,
            report,
            agent,
            similarity=hit.similarity,
            timeout_seconds=remaining_seconds,
        )
        if updated is not None:
            return updated

    if merge_candidates:
        logger.info("All %d existing playbooks are up-to-date", merge_candidates)

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
