from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.agent_factory import create_evidence_collection_agent  # noqa: F401
from rca_agent.config.settings import (
    EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
    S3_EVIDENCE_BUCKET,
    S3_EVIDENCE_MAX_RETRIES,
)
from rca_agent.ports.dto.models import Hypothesis, HypothesisStatus, ScopingResult
from rca_agent.prompts.evidence import EVIDENCE_COLLECTION_USER_PROMPT_TEMPLATE
from rca_agent.utils.retry import retry_with_backoff

if TYPE_CHECKING:
    from strands import Agent
    from strands.tools.mcp import MCPClient

logger = logging.getLogger(__name__)

_SUMMARY_MAX_LEN = 500


class EvidenceOutput(BaseModel):
    """Structured output from the evidence collection agent."""

    metrics_evidence: str = ""
    logs_evidence: str = ""
    deploy_evidence: str = ""
    code_change_evidence: str = ""
    combined_summary: str = ""


class EvidenceCollectionResult(BaseModel):
    hypothesis_id: str
    summary: str
    full_evidence: str
    evidence_types: list[str] = Field(default_factory=list)
    failed: bool = False


class EvidenceCollectionSummary(BaseModel):
    evidence_map: dict[str, str] = Field(default_factory=dict)
    failed_ids: set[str] = Field(default_factory=set)


EVIDENCE_FAILED_SENTINEL = "Evidence collection timed out or failed."


def _build_parent_context(
    hypothesis: Hypothesis,
    hypotheses_by_id: dict[str, Hypothesis] | None,
    evidence_map: dict[str, str] | None,
) -> str:
    if not hypothesis.parent_id or not hypotheses_by_id or not evidence_map:
        return ""
    parent = hypotheses_by_id.get(hypothesis.parent_id)
    if parent is None:
        return ""

    if parent.status == HypothesisStatus.REJECTED:
        return f"\n## Parent Hypothesis (REJECTED)\n- **Description**: {parent.description}\n- **Status**: REJECTED\n"

    parent_summary = evidence_map.get(parent.hypothesis_id, "")
    if not parent_summary:
        return ""

    return (
        f"\n## Parent Hypothesis Evidence\n"
        f"- **Description**: {parent.description}\n"
        f"- **Category**: {parent.category}\n"
        f"- **Evidence Summary**:\n{parent_summary}\n"
    )


def _build_user_prompt(
    hypothesis: Hypothesis,
    scoping_result: ScopingResult,
    *,
    hypotheses_by_id: dict[str, Hypothesis] | None = None,
    evidence_map: dict[str, str] | None = None,
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

    parent_context = _build_parent_context(hypothesis, hypotheses_by_id, evidence_map)

    return EVIDENCE_COLLECTION_USER_PROMPT_TEMPLATE.format(
        alarm_name=alarm.alarm_name if alarm else "N/A",
        alarm_region=alarm.region if alarm else "us-east-1",
        service_name=alarm.service_name if alarm else "N/A",
        resource_id=alarm.resource_id if alarm else "N/A",
        state_change_time=alarm.state_change_time if alarm else "N/A",
        blast_radius=scoping_result.blast_radius,
        initial_severity=scoping_result.initial_severity,
        metric_context=metric_context or "No metric data available.",
        parent_context=parent_context,
        hypothesis_description=hypothesis.description,
        hypothesis_category=hypothesis.category,
        required_evidence="\n".join(f"- {e}" for e in hypothesis.required_evidence) or "N/A",
    )


def _invoke_agent(agent: Agent, prompt: str) -> EvidenceOutput:
    result = agent(prompt, structured_output_model=EvidenceOutput)
    return result.structured_output


def _build_full_evidence(output: EvidenceOutput) -> tuple[str, list[str]]:
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

    return combined, evidence_types


def collect_evidence(
    hypothesis: Hypothesis,
    scoping_result: ScopingResult,
    *,
    mcp_clients: list[MCPClient] | None = None,
    timeout_seconds: int = EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
    hypotheses_by_id: dict[str, Hypothesis] | None = None,
    evidence_map: dict[str, str] | None = None,
) -> EvidenceCollectionResult:
    agent = create_evidence_collection_agent(mcp_clients=mcp_clients)
    user_prompt = _build_user_prompt(
        hypothesis,
        scoping_result,
        hypotheses_by_id=hypotheses_by_id,
        evidence_map=evidence_map,
    )
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
            summary=EVIDENCE_FAILED_SENTINEL,
            full_evidence=EVIDENCE_FAILED_SENTINEL,
            failed=True,
        )

    full_evidence, evidence_types = _build_full_evidence(output)
    summary = (output.combined_summary or full_evidence)[:_SUMMARY_MAX_LEN]

    logger.info(
        "Evidence collected for %s: types=%s",
        hypothesis.hypothesis_id,
        evidence_types,
    )

    return EvidenceCollectionResult(
        hypothesis_id=hypothesis.hypothesis_id,
        summary=summary,
        full_evidence=full_evidence,
        evidence_types=evidence_types,
    )


def run_evidence_collection(
    hypotheses: list[Hypothesis],
    scoping_result: ScopingResult,
    *,
    mcp_clients: list[MCPClient] | None = None,
    timeout_seconds: int = EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
    rca_id: str = "",
    trace=None,
    s3_client=None,
    existing_evidence_map: dict[str, str] | None = None,
    all_hypotheses: list[Hypothesis] | None = None,
    cancel_checker=None,
) -> EvidenceCollectionSummary:
    lookup_map: dict[str, str] = {}
    if existing_evidence_map:
        lookup_map.update(existing_evidence_map)
    new_evidence_map: dict[str, str] = {}
    failed_ids: set[str] = set()
    source = all_hypotheses if all_hypotheses else hypotheses
    hypotheses_by_id = {h.hypothesis_id: h for h in source}

    for h in hypotheses:
        if cancel_checker is not None:
            cancel_checker()
        result = collect_evidence(
            h,
            scoping_result,
            mcp_clients=mcp_clients,
            timeout_seconds=timeout_seconds,
            hypotheses_by_id=hypotheses_by_id,
            evidence_map=lookup_map,
        )

        lookup_map[h.hypothesis_id] = result.summary
        new_evidence_map[h.hypothesis_id] = result.summary

        if result.failed:
            failed_ids.add(h.hypothesis_id)

        if trace:
            trace.update_hypothesis_evidence(h.hypothesis_id, evidence_summary=result.summary)

        if rca_id and not result.failed:
            _save_single_evidence_to_s3(rca_id, h.hypothesis_id, result.full_evidence, s3_client=s3_client)

    return EvidenceCollectionSummary(evidence_map=new_evidence_map, failed_ids=failed_ids)


def _save_single_evidence_to_s3(
    rca_id: str,
    hypothesis_id: str,
    evidence_text: str,
    *,
    s3_client=None,
    max_retries: int = S3_EVIDENCE_MAX_RETRIES,
    base_delay: float = 1.0,
) -> str | None:
    if not S3_EVIDENCE_BUCKET or s3_client is None:
        return None
    if not evidence_text.strip():
        return None

    key = f"rca/{rca_id}/evidence/{hypothesis_id}/combined.md"

    def put() -> str:
        s3_client.put_object(
            Bucket=S3_EVIDENCE_BUCKET,
            Key=key,
            Body=evidence_text,
            ContentType="text/markdown",
        )
        logger.info("Evidence saved: s3://%s/%s", S3_EVIDENCE_BUCKET, key)
        return key

    return retry_with_backoff(
        put,
        max_retries=max_retries,
        base_delay=base_delay,
        operation=f"evidence save for {hypothesis_id}",
    )


def save_evidence_to_s3(
    rca_id: str,
    evidence_map: dict[str, str],
    *,
    s3_client=None,
    max_retries: int = S3_EVIDENCE_MAX_RETRIES,
    base_delay: float = 1.0,
) -> list[str]:
    if not S3_EVIDENCE_BUCKET or s3_client is None:
        logger.info("S3 evidence bucket not configured, skipping upload")
        return []

    saved_keys = []
    for hypothesis_id, evidence_text in evidence_map.items():
        key = _save_single_evidence_to_s3(
            rca_id,
            hypothesis_id,
            evidence_text,
            s3_client=s3_client,
            max_retries=max_retries,
            base_delay=base_delay,
        )
        if key:
            saved_keys.append(key)

    return saved_keys
