from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config import (
    LLM_DEFAULT_TIMEOUT_SECONDS,
    PLAYBOOK_UPDATE_THRESHOLD,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_PLAYBOOK_INDEX,
)
from rca_agent.embeddings import embed_document, embed_query
from rca_agent.models import Playbook, RcaReport, ScopingResult
from rca_agent.prompts import (
    PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE,
    PLAYBOOK_USER_PROMPT_TEMPLATE,
)

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

_SEARCH_MAX_RETRIES = 3
_SEARCH_BASE_DELAY = 1.0


class PlaybookOutput(BaseModel):
    failure_type: str
    symptom_pattern: str
    verification_steps: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class PlaybookUpdateOutput(BaseModel):
    needs_update: bool = True
    failure_type: str = ""
    symptom_pattern: str = ""
    verification_steps: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class _ExistingPlaybookHit(BaseModel):
    playbook_id: str
    similarity: float
    failure_type: str
    symptom_pattern: str
    verification_steps: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def _build_embed_key(report: RcaReport, scoping_result: ScopingResult | None) -> str:
    metric_name = ""
    if scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
        metric_name = scoping_result.raw_alarm.trigger.metric_name
    parts = [
        report.root_cause or "unknown",
        metric_name,
        report.incident_summary,
    ]
    return " | ".join(p for p in parts if p)


def _build_user_prompt(report: RcaReport) -> str:
    return PLAYBOOK_USER_PROMPT_TEMPLATE.format(
        failure_type="Inferred from root cause",
        root_cause=report.root_cause,
        severity="high" if report.root_cause_confirmed else "medium",
        evidence_highlights="\n".join(f"- {e}" for e in report.evidence_list[:5]) or "N/A",
        mitigation_text=report.temporary_mitigation or "N/A",
        remediation_text=report.permanent_remediation or "N/A",
    )


def _build_update_prompt(existing: _ExistingPlaybookHit, report: RcaReport) -> str:
    return PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE.format(
        existing_failure_type=existing.failure_type,
        existing_symptom_pattern=existing.symptom_pattern,
        existing_verification_steps="\n".join(f"  - {s}" for s in existing.verification_steps) or "N/A",
        existing_temporary_mitigation=existing.temporary_mitigation or "N/A",
        existing_permanent_remediation=existing.permanent_remediation or "N/A",
        existing_prevention_measures="\n".join(f"  - {m}" for m in existing.prevention_measures) or "N/A",
        root_cause=report.root_cause,
        severity="high" if report.root_cause_confirmed else "medium",
        evidence_highlights="\n".join(f"  - {e}" for e in report.evidence_list[:5]) or "N/A",
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
    s3_vectors_client=None,
    threshold: float = PLAYBOOK_UPDATE_THRESHOLD,
    max_retries: int = _SEARCH_MAX_RETRIES,
    base_delay: float = _SEARCH_BASE_DELAY,
) -> list[_ExistingPlaybookHit]:
    if not S3_VECTOR_BUCKET_NAME or s3_vectors_client is None:
        return []

    query_text = _build_embed_key(report, scoping_result)
    try:
        query_vector = embed_query(query_text)
    except Exception:
        logger.exception("Failed to embed query text, skipping playbook search")
        return []

    response = None
    for attempt in range(max_retries):
        try:
            response = s3_vectors_client.query_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_PLAYBOOK_INDEX,
                queryVector={"float32": query_vector},
                topK=3,
            )
            break
        except Exception:
            if attempt == max_retries - 1:
                logger.exception("Failed to search playbooks after %d attempts", max_retries)
                return []
            delay = base_delay * (2**attempt)
            logger.warning("Playbook search attempt %d failed, retrying in %.1fs", attempt + 1, delay)
            time.sleep(delay)

    if response is None:
        return []

    hits = []
    for item in response.get("vectors", []):
        similarity = item.get("distance", 0.0)
        if similarity < threshold:
            continue
        metadata = item.get("metadata", {})
        hits.append(
            _ExistingPlaybookHit(
                playbook_id=item.get("key", ""),
                similarity=similarity,
                failure_type=metadata.get("failure_type", ""),
                symptom_pattern=metadata.get("symptom_pattern", ""),
                verification_steps=metadata.get("verification_steps", []),
                temporary_mitigation=metadata.get("temporary_mitigation", ""),
                permanent_remediation=metadata.get("permanent_remediation", ""),
                prevention_measures=metadata.get("prevention_measures", []),
                tags=metadata.get("tags", []),
            )
        )
    return hits


def _try_update_existing(
    hit: _ExistingPlaybookHit,
    report: RcaReport,
    update_agent: Agent,
    *,
    timeout_seconds: int = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Playbook | None:
    prompt = _build_update_prompt(hit, report)
    logger.info(
        "Checking update for playbook %s (similarity=%.2f)",
        hit.playbook_id,
        hit.similarity,
    )

    output: PlaybookUpdateOutput | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke_update_agent, update_agent, prompt)
        try:
            output = future.result(timeout=timeout_seconds)
        except (FuturesTimeoutError, Exception):
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
        verification_steps=output.verification_steps or hit.verification_steps,
        temporary_mitigation=output.temporary_mitigation or hit.temporary_mitigation,
        permanent_remediation=output.permanent_remediation or hit.permanent_remediation,
        prevention_measures=output.prevention_measures or hit.prevention_measures,
        rca_id=report.rca_id,
        tags=output.tags or hit.tags,
    )


def run_playbook_generation(
    report: RcaReport,
    agent: Agent,
    *,
    scoping_result: ScopingResult | None = None,
    s3_vectors_client=None,
    timeout_seconds: int = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Playbook:
    existing_hits = search_existing_playbooks(
        report,
        scoping_result,
        s3_vectors_client=s3_vectors_client,
    )

    for hit in existing_hits:
        updated = _try_update_existing(hit, report, agent, timeout_seconds=timeout_seconds)
        if updated is not None:
            return updated

    if existing_hits:
        logger.info("All %d existing playbooks are up-to-date", len(existing_hits))

    playbook_id = str(uuid.uuid4())
    user_prompt = _build_user_prompt(report)

    logger.info("Generating new playbook from RCA %s", report.rca_id)

    output: PlaybookOutput | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke_agent, agent, user_prompt)
        try:
            output = future.result(timeout=timeout_seconds)
        except (FuturesTimeoutError, Exception):
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
        verification_steps=output.verification_steps,
        temporary_mitigation=output.temporary_mitigation,
        permanent_remediation=output.permanent_remediation,
        prevention_measures=output.prevention_measures,
        rca_id=report.rca_id,
        tags=output.tags,
    )


def save_playbook_to_s3_vectors(
    playbook: Playbook,
    *,
    scoping_result: ScopingResult | None = None,
    s3_vectors_client=None,
) -> bool:
    if not S3_VECTOR_BUCKET_NAME or s3_vectors_client is None:
        logger.info("S3 Vectors not configured, skipping playbook indexing")
        return False

    metric_name = ""
    if scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
        metric_name = scoping_result.raw_alarm.trigger.metric_name

    embed_text = " | ".join(p for p in [playbook.failure_type, metric_name, playbook.symptom_pattern] if p)
    try:
        vector = embed_document(embed_text)
    except Exception:
        logger.exception("Failed to embed playbook text")
        return False

    metadata = {
        "failure_type": playbook.failure_type,
        "symptom_pattern": playbook.symptom_pattern,
        "verification_steps": playbook.verification_steps,
        "temporary_mitigation": playbook.temporary_mitigation,
        "permanent_remediation": playbook.permanent_remediation,
        "prevention_measures": playbook.prevention_measures,
        "tags": playbook.tags,
        "rca_id": playbook.rca_id,
    }

    try:
        s3_vectors_client.put_vectors(
            vectorBucketName=S3_VECTOR_BUCKET_NAME,
            indexName=S3_VECTOR_PLAYBOOK_INDEX,
            vectors=[
                {
                    "key": playbook.playbook_id,
                    "data": {"float32": vector},
                    "metadata": metadata,
                }
            ],
        )
        logger.info("Playbook %s indexed in S3 Vectors", playbook.playbook_id)
        return True
    except Exception:
        logger.exception("Failed to index playbook in S3 Vectors")
        return False
