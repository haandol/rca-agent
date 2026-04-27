from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config import (
    PLAYBOOK_SIMILARITY_THRESHOLD,
    PLAYBOOK_TOP_K,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_PLAYBOOK_INDEX,
    SCOPING_TIMEOUT_SECONDS,
)
from rca_agent.embeddings import embed_query
from rca_agent.models import AlarmPayload, PlaybookMatch, ScopingResult
from rca_agent.prompts import SCOPING_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

_PLAYBOOK_SEARCH_MAX_RETRIES = 3
_PLAYBOOK_SEARCH_BASE_DELAY = 1.0


class ScopingOutput(BaseModel):
    """Structured output model for the scoping agent."""

    alarm_summary: str
    anomaly_start_time: str | None = None
    blast_radius: str = "single"
    initial_severity: str = "medium"
    metric_snapshot: dict[str, dict] = Field(default_factory=dict)


def _build_playbook_context(playbooks: list[PlaybookMatch]) -> str:
    if not playbooks:
        return "No similar playbooks found."
    lines = ["## Similar Playbooks (from past incidents)"]
    for i, pb in enumerate(playbooks, 1):
        lines.append(f"{i}. **{pb.title}** (similarity: {pb.similarity:.2f})")
        if pb.root_cause_summary:
            lines.append(f"   Root cause: {pb.root_cause_summary}")
    return "\n".join(lines)


def _build_user_prompt(alarm: AlarmPayload, playbooks: list[PlaybookMatch]) -> str:
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
        playbook_context=_build_playbook_context(playbooks),
    )


def search_similar_playbooks(
    alarm: AlarmPayload,
    *,
    s3_vectors_client=None,
    max_retries: int = _PLAYBOOK_SEARCH_MAX_RETRIES,
    base_delay: float = _PLAYBOOK_SEARCH_BASE_DELAY,
) -> list[PlaybookMatch]:
    """Search S3 Vectors for similar playbooks based on alarm context.

    Retries with exponential backoff on transient failures.
    """
    if not S3_VECTOR_BUCKET_NAME or s3_vectors_client is None:
        logger.info("S3 Vectors not configured, skipping playbook search")
        return []

    query_text = f"{alarm.service_name} {alarm.alarm_name} {alarm.new_state_reason}"
    query_vector = embed_query(query_text)

    for attempt in range(max_retries):
        try:
            response = s3_vectors_client.query_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_PLAYBOOK_INDEX,
                queryVector={"float32": query_vector},
                topK=PLAYBOOK_TOP_K,
            )
            break
        except Exception:
            if attempt == max_retries - 1:
                logger.exception("Failed to search playbooks after %d attempts", max_retries)
                return []
            delay = base_delay * (2**attempt)
            logger.warning("Playbook search attempt %d failed, retrying in %.1fs", attempt + 1, delay)
            time.sleep(delay)

    matches = []
    for item in response.get("vectors", []):
        similarity = item.get("distance", 0.0)
        if similarity < PLAYBOOK_SIMILARITY_THRESHOLD:
            continue
        metadata = item.get("metadata", {})
        matches.append(
            PlaybookMatch(
                playbook_id=item.get("key", ""),
                title=metadata.get("title", "Unknown"),
                similarity=similarity,
                root_cause_summary=metadata.get("root_cause_summary", ""),
            )
        )
    return matches


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
    s3_vectors_client=None,
    timeout_seconds: int = SCOPING_TIMEOUT_SECONDS,
) -> ScopingResult:
    """Run the initial scoping phase for an alarm.

    1. Search S3 Vectors for similar playbooks
    2. Build the prompt with alarm context + playbook references
    3. Invoke the Strands agent with CloudWatch MCP tools (with timeout)
    4. Parse structured output into ScopingResult

    If the agent exceeds timeout_seconds, returns a fallback ScopingResult
    built from the alarm payload alone.
    """
    playbooks = search_similar_playbooks(alarm, s3_vectors_client=s3_vectors_client)
    user_prompt = _build_user_prompt(alarm, playbooks)

    logger.info("Running scoping agent for alarm: %s (timeout=%ds)", alarm.alarm_name, timeout_seconds)

    output: ScopingOutput | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke_scoping_agent, agent, user_prompt)
        try:
            output = future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            logger.warning("Scoping agent timed out after %ds, using alarm payload as fallback", timeout_seconds)
            future.cancel()
        except Exception:
            logger.exception("Scoping agent failed")

    if output is None:
        return ScopingResult(
            alarm_summary=f"[Timeout] {alarm.alarm_name}: {alarm.new_state_reason}",
            blast_radius="single",
            initial_severity="medium",
            similar_playbooks=playbooks,
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
        similar_playbooks=playbooks,
        raw_alarm=alarm,
    )
