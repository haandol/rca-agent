from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import (
    REPORT_SIMILARITY_THRESHOLD,
    REPORT_TOP_K,
    S3_VECTOR_BUCKET_NAME,
    S3_VECTOR_REPORT_INDEX,
    SCOPING_TIMEOUT_SECONDS,
)
from rca_agent.embeddings import embed_query
from rca_agent.ports.dto.models import AlarmPayload, ReportMatch, ScopingResult
from rca_agent.prompts.scoping import SCOPING_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

_REPORT_SEARCH_MAX_RETRIES = 3
_REPORT_SEARCH_BASE_DELAY = 1.0


class ScopingOutput(BaseModel):
    """Structured output model for the scoping agent."""

    alarm_summary: str
    anomaly_start_time: str | None = None
    blast_radius: str = "single"
    initial_severity: str = "medium"
    metric_snapshot: dict[str, dict] = Field(default_factory=dict)


def _build_report_context(reports: list[ReportMatch]) -> str:
    if not reports:
        return "No similar past RCA reports found."
    lines = ["## Similar Past RCA Reports"]
    for i, r in enumerate(reports, 1):
        status = "confirmed" if r.confirmed else "unconfirmed"
        lines.append(f"{i}. **{r.root_cause}** (similarity: {r.similarity:.2f}, {status})")
        if r.incident_summary:
            lines.append(f"   Incident: {r.incident_summary}")
    return "\n".join(lines)


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
        report_context=_build_report_context(reports),
    )


def search_similar_reports(
    alarm: AlarmPayload,
    *,
    s3_vectors_client=None,
    max_retries: int = _REPORT_SEARCH_MAX_RETRIES,
    base_delay: float = _REPORT_SEARCH_BASE_DELAY,
) -> list[ReportMatch]:
    if not S3_VECTOR_BUCKET_NAME or s3_vectors_client is None:
        logger.info("S3 Vectors not configured, skipping report search")
        return []

    reason = alarm.new_state_reason[:80] if alarm.new_state_reason else ""
    metric = alarm.trigger.metric_name[:80] if alarm.trigger else ""
    query_text = f"장애유형: {alarm.alarm_name[:80]} | 증상: {reason} | 메트릭: {metric}"
    try:
        query_vector = embed_query(query_text)
    except Exception:
        logger.exception("Failed to embed query text, skipping report search")
        return []

    for attempt in range(max_retries):
        try:
            response = s3_vectors_client.query_vectors(
                vectorBucketName=S3_VECTOR_BUCKET_NAME,
                indexName=S3_VECTOR_REPORT_INDEX,
                queryVector={"float32": query_vector},
                topK=REPORT_TOP_K,
            )
            break
        except Exception:
            if attempt == max_retries - 1:
                logger.exception("Failed to search reports after %d attempts", max_retries)
                return []
            delay = base_delay * (2**attempt)
            logger.warning("Report search attempt %d failed, retrying in %.1fs", attempt + 1, delay)
            time.sleep(delay)

    matches = []
    for item in response.get("vectors", []):
        similarity = item.get("distance", 0.0)
        if similarity < REPORT_SIMILARITY_THRESHOLD:
            continue
        metadata = item.get("metadata", {})
        matches.append(
            ReportMatch(
                rca_id=item.get("key", ""),
                similarity=similarity,
                incident_summary=metadata.get("incident_summary", ""),
                root_cause=metadata.get("root_cause", ""),
                hypothesis_path=metadata.get("hypothesis_path", ""),
                confirmed=metadata.get("confirmed", "false") == "true",
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
    reports = search_similar_reports(alarm, s3_vectors_client=s3_vectors_client)
    user_prompt = _build_user_prompt(alarm, reports)

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
