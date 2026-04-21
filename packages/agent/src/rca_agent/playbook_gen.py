from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config import (
    LLM_DEFAULT_TIMEOUT_SECONDS,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_PLAYBOOK_INDEX,
)
from rca_agent.models import Playbook, RcaReport
from rca_agent.prompts import PLAYBOOK_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


class PlaybookOutput(BaseModel):
    failure_type: str
    symptom_pattern: str
    verification_steps: list[str] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def _build_user_prompt(report: RcaReport) -> str:
    return PLAYBOOK_USER_PROMPT_TEMPLATE.format(
        failure_type="Inferred from root cause",
        root_cause=report.root_cause,
        severity="high" if report.root_cause_confirmed else "medium",
        evidence_highlights="\n".join(f"- {e}" for e in report.evidence_list[:5]) or "N/A",
        mitigation_text=report.temporary_mitigation or "N/A",
        remediation_text=report.permanent_remediation or "N/A",
    )


def _invoke_agent(agent: Agent, prompt: str) -> PlaybookOutput:
    result = agent(prompt, structured_output_model=PlaybookOutput)
    return result.structured_output


def run_playbook_generation(
    report: RcaReport,
    agent: Agent,
    *,
    timeout_seconds: int = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Playbook:
    playbook_id = str(uuid.uuid4())
    user_prompt = _build_user_prompt(report)

    logger.info("Generating playbook from RCA %s", report.rca_id)

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
    s3_vectors_client=None,
) -> bool:
    if not S3_VECTOR_BUCKET_NAME or s3_vectors_client is None:
        logger.info("S3 Vectors not configured, skipping playbook indexing")
        return False

    embed_text = f"{playbook.failure_type} {playbook.symptom_pattern} {' '.join(playbook.tags)}"
    metadata = {
        "title": playbook.failure_type,
        "root_cause_summary": playbook.symptom_pattern,
        "rca_id": playbook.rca_id,
    }

    try:
        s3_vectors_client.put_vectors(
            vectorBucketName=S3_VECTOR_BUCKET_NAME,
            indexName=S3_VECTOR_PLAYBOOK_INDEX,
            vectors=[
                {
                    "key": playbook.playbook_id,
                    "data": {"text": embed_text},
                    "metadata": metadata,
                }
            ],
        )
        logger.info("Playbook %s indexed in S3 Vectors", playbook.playbook_id)
        return True
    except Exception:
        logger.exception("Failed to index playbook in S3 Vectors")
        return False
