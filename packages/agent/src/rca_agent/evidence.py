from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config import (
    EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
    S3_EVIDENCE_BUCKET,
    S3_EVIDENCE_MAX_RETRIES,
)
from rca_agent.models import Hypothesis, ScopingResult
from rca_agent.prompts import EVIDENCE_COLLECTION_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

_S3_BASE_DELAY = 1.0


class EvidenceOutput(BaseModel):
    """Structured output from the evidence collection agent."""

    metrics_evidence: str = ""
    logs_evidence: str = ""
    deploy_evidence: str = ""
    code_change_evidence: str = ""
    combined_summary: str = ""


class EvidenceCollectionResult(BaseModel):
    hypothesis_id: str
    evidence_text: str
    evidence_types: list[str] = Field(default_factory=list)
    s3_keys: list[str] = Field(default_factory=list)


def _build_user_prompt(
    hypothesis: Hypothesis,
    scoping_result: ScopingResult,
) -> str:
    alarm = scoping_result.raw_alarm
    metric_context = ""
    if scoping_result.metric_snapshot:
        lines = []
        for name, data in scoping_result.metric_snapshot.items():
            current = data.get("current", "N/A")
            baseline = data.get("baseline", "N/A")
            unit = data.get("unit", "")
            lines.append(f"- {name}: current={current}, baseline={baseline} {unit}")
        metric_context = "\n".join(lines)

    return EVIDENCE_COLLECTION_USER_PROMPT_TEMPLATE.format(
        alarm_name=alarm.alarm_name if alarm else "N/A",
        alarm_region=alarm.region if alarm else "us-east-1",
        service_name=alarm.service_name if alarm else "N/A",
        resource_id=alarm.resource_id if alarm else "N/A",
        state_change_time=alarm.state_change_time if alarm else "N/A",
        blast_radius=scoping_result.blast_radius,
        initial_severity=scoping_result.initial_severity,
        metric_context=metric_context or "No metric data available.",
        hypothesis_description=hypothesis.description,
        hypothesis_category=hypothesis.category,
        required_evidence="\n".join(f"- {e}" for e in hypothesis.required_evidence) or "N/A",
    )


def _invoke_agent(agent: Agent, prompt: str) -> EvidenceOutput:
    result = agent(prompt, structured_output_model=EvidenceOutput)
    return result.structured_output


def collect_evidence(
    hypothesis: Hypothesis,
    scoping_result: ScopingResult,
    agent: Agent,
    *,
    timeout_seconds: int = EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
) -> EvidenceCollectionResult:
    user_prompt = _build_user_prompt(hypothesis, scoping_result)
    logger.info(
        "Collecting evidence for hypothesis %s: %s",
        hypothesis.hypothesis_id,
        hypothesis.description[:60],
    )

    output: EvidenceOutput | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke_agent, agent, user_prompt)
        try:
            output = future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            logger.warning("Evidence collection timed out for %s", hypothesis.hypothesis_id)
            future.cancel()
        except Exception:
            logger.exception("Evidence collection failed for %s", hypothesis.hypothesis_id)

    if output is None:
        return EvidenceCollectionResult(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_text="Evidence collection timed out or failed.",
        )

    evidence_types = []
    sections = []
    if output.metrics_evidence:
        evidence_types.append("metrics")
        sections.append(f"## Metrics Evidence\n{output.metrics_evidence}")
    if output.logs_evidence:
        evidence_types.append("logs")
        sections.append(f"## Logs Evidence\n{output.logs_evidence}")
    if output.deploy_evidence:
        evidence_types.append("deploy_history")
        sections.append(f"## Deploy/Change Evidence\n{output.deploy_evidence}")
    if output.code_change_evidence:
        evidence_types.append("code_change")
        sections.append(f"## Code Change Evidence\n{output.code_change_evidence}")

    combined = "\n\n".join(sections)
    if output.combined_summary:
        combined += f"\n\n## Summary\n{output.combined_summary}"

    if not combined.strip():
        combined = "No evidence could be collected for this hypothesis."

    logger.info(
        "Evidence collected for %s: types=%s",
        hypothesis.hypothesis_id,
        evidence_types,
    )

    return EvidenceCollectionResult(
        hypothesis_id=hypothesis.hypothesis_id,
        evidence_text=combined,
        evidence_types=evidence_types,
    )


def run_evidence_collection(
    hypotheses: list[Hypothesis],
    scoping_result: ScopingResult,
    agent: Agent,
    *,
    timeout_seconds: int = EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
) -> dict[str, str]:
    evidence_map: dict[str, str] = {}

    for h in hypotheses:
        result = collect_evidence(h, scoping_result, agent, timeout_seconds=timeout_seconds)
        evidence_map[h.hypothesis_id] = result.evidence_text

    return evidence_map


def save_evidence_to_s3(
    rca_id: str,
    evidence_map: dict[str, str],
    *,
    s3_client=None,
    max_retries: int = S3_EVIDENCE_MAX_RETRIES,
    base_delay: float = _S3_BASE_DELAY,
) -> list[str]:
    if not S3_EVIDENCE_BUCKET or s3_client is None:
        logger.info("S3 evidence bucket not configured, skipping upload")
        return []

    saved_keys = []
    for hypothesis_id, evidence_text in evidence_map.items():
        if not evidence_text.strip():
            continue
        key = f"rca/{rca_id}/evidence/{hypothesis_id}/combined.md"
        for attempt in range(max_retries):
            try:
                s3_client.put_object(
                    Bucket=S3_EVIDENCE_BUCKET,
                    Key=key,
                    Body=evidence_text,
                    ContentType="text/markdown",
                )
                saved_keys.append(key)
                logger.info("Evidence saved: s3://%s/%s", S3_EVIDENCE_BUCKET, key)
                break
            except Exception:
                if attempt == max_retries - 1:
                    logger.exception("Failed to save evidence for %s after %d attempts", hypothesis_id, max_retries)
                else:
                    delay = base_delay * (2**attempt)
                    logger.warning("Evidence save attempt %d failed, retrying in %.1fs", attempt + 1, delay)
                    time.sleep(delay)

    return saved_keys
