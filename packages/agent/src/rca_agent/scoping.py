from __future__ import annotations

import json
import logging
from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config import (
    PLAYBOOK_SIMILARITY_THRESHOLD,
    PLAYBOOK_TOP_K,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_PLAYBOOK_INDEX,
)
from rca_agent.models import AlarmPayload, PlaybookMatch, ScopingResult
from rca_agent.prompts import SCOPING_USER_PROMPT_TEMPLATE

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
) -> list[PlaybookMatch]:
    """Search S3 Vectors for similar playbooks based on alarm context."""
    if not S3_VECTOR_BUCKET_NAME or s3_vectors_client is None:
        logger.info("S3 Vectors not configured, skipping playbook search")
        return []

    query_text = f"{alarm.service_name} {alarm.alarm_name} {alarm.new_state_reason}"
    try:
        response = s3_vectors_client.query_vectors(
            vectorBucketName=S3_VECTOR_BUCKET_NAME,
            indexName=S3_VECTOR_PLAYBOOK_INDEX,
            queryText=query_text,
            topK=PLAYBOOK_TOP_K,
        )
    except Exception:
        logger.exception("Failed to search playbooks from S3 Vectors")
        return []

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


def run_scoping(
    alarm: AlarmPayload,
    agent: Agent,
    *,
    s3_vectors_client=None,
) -> ScopingResult:
    """Run the initial scoping phase for an alarm.

    1. Search S3 Vectors for similar playbooks
    2. Build the prompt with alarm context + playbook references
    3. Invoke the Strands agent with CloudWatch MCP tools
    4. Parse structured output into ScopingResult
    """
    playbooks = search_similar_playbooks(alarm, s3_vectors_client=s3_vectors_client)
    user_prompt = _build_user_prompt(alarm, playbooks)

    logger.info("Running scoping agent for alarm: %s", alarm.alarm_name)
    result = agent(user_prompt, structured_output_model=ScopingOutput)
    output: ScopingOutput = result.structured_output
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
