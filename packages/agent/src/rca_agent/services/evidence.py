from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.agent_factory import create_evidence_collection_agent  # noqa: F401
from rca_agent.config.aws_sdk import EVIDENCE_SAVE_BASE_DELAY_SECONDS
from rca_agent.config.settings import (
    EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
    S3_EVIDENCE_BUCKET,
    S3_EVIDENCE_MAX_RETRIES,
)
from rca_agent.ports.dto.models import Hypothesis, HypothesisStatus, ScopingResult
from rca_agent.ports.dto.observations import CriticalFact
from rca_agent.prompts.evidence import EVIDENCE_COLLECTION_USER_PROMPT_TEMPLATE
from rca_agent.services.collected_observations import (
    archive_received_outputs,
    derive_received_facts,
    received_tool_warnings,
    render_collected_facts,
)
from rca_agent.services.observation_context import (
    render_alarm_description,
    render_concurrent_alarms,
    render_critical_facts,
    render_observations,
)
from rca_agent.utils.agent_invocation import InvocationNotStartedError, invoke_agent
from rca_agent.utils.retry import retry_with_backoff

if TYPE_CHECKING:
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
    retryable: bool = False
    external_request_finished: bool = True
    diagnostic_partial: str = ""
    failure_reason: str = ""
    invocation_not_started: bool = False
    critical_facts: list[CriticalFact] = Field(default_factory=list)
    raw_tool_outputs: str = ""
    collection_warnings: list[dict] = Field(default_factory=list)


class CollectionStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    COMPLETE = "COMPLETE"


class CollectionAttempt(BaseModel):
    """Attempt ownership prevents an expired result from becoming reusable evidence."""

    status: CollectionStatus = CollectionStatus.NOT_STARTED
    attempts: int = 0
    retryable: bool = False
    scope_key: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    budget_seconds: float = 0
    external_request_finished: bool = True
    reason: str = ""

    def eligible(self) -> bool:
        """Only a finished transient failure may consume the one retry."""
        return self.status == CollectionStatus.NOT_STARTED or (
            self.status == CollectionStatus.FAILED
            and self.retryable
            and self.attempts < 2
            and self.external_request_finished
        )


class EvidenceCollectionSummary(BaseModel):
    full_evidence_map: dict[str, str] = Field(default_factory=dict)
    evidence_map: dict[str, str] = Field(default_factory=dict)
    failed_ids: set[str] = Field(default_factory=set)
    collection_states: dict[str, CollectionAttempt] = Field(default_factory=dict)
    fact_map: dict[str, list[CriticalFact]] = Field(default_factory=dict)
    source_ref_map: dict[str, list[str]] = Field(default_factory=dict)
    warning_map: dict[str, list[dict]] = Field(default_factory=dict)


EVIDENCE_FAILED_SENTINEL = "Evidence collection timed out or failed."


def evidence_scope_key(scoping: ScopingResult) -> str:
    """Completed evidence is reusable only for the exact incident and observation scope."""
    return hashlib.sha256(scoping.model_dump_json().encode()).hexdigest()


def collection_is_due(record: CollectionAttempt | None, scope_key: str, summary: str | None) -> bool:
    """A stale success cannot hide new-scope work, and unknown requests must never overlap."""
    if record is None:
        return summary is None or summary == EVIDENCE_FAILED_SENTINEL
    if record.scope_key != scope_key:
        return record.external_request_finished and record.status != CollectionStatus.RUNNING
    return record.eligible()


def _build_parent_context(
    hypothesis: Hypothesis,
    hypotheses_by_id: dict[str, Hypothesis] | None,
    evidence_map: dict[str, str] | None,
    fact_map: dict[str, list[CriticalFact]] | None = None,
    source_ref_map: dict[str, list[str]] | None = None,
    warning_map: dict[str, list[dict]] | None = None,
) -> str:
    """Transmit parent prose and separately verified source facts without its original responses."""
    if not hypothesis.parent_id or not hypotheses_by_id or not evidence_map:
        return ""
    parent = hypotheses_by_id.get(hypothesis.parent_id)
    if parent is None:
        return ""

    if parent.status == HypothesisStatus.REJECTED:
        return f"\n## Parent Hypothesis (REJECTED)\n- **Description**: {parent.description}\n- **Status**: REJECTED\n"

    parent_summary = evidence_map.get(parent.hypothesis_id, "")
    facts = (fact_map or {}).get(parent.hypothesis_id, [])
    references = (source_ref_map or {}).get(parent.hypothesis_id, [])
    warnings = (warning_map or {}).get(parent.hypothesis_id, [])
    if not parent_summary and not facts and not references and not warnings:
        return ""

    return (
        f"\n## Parent Hypothesis Evidence\n"
        f"- **Description**: {parent.description}\n"
        f"- **Category**: {parent.category}\n"
        f"- **Evidence Summary**:\n{parent_summary[:_SUMMARY_MAX_LEN]}\n"
    ) + render_collected_facts(facts, references, warnings)


def _build_user_prompt(
    hypothesis: Hypothesis,
    scoping_result: ScopingResult,
    *,
    hypotheses_by_id: dict[str, Hypothesis] | None = None,
    evidence_map: dict[str, str] | None = None,
    fact_map: dict[str, list[CriticalFact]] | None = None,
    source_ref_map: dict[str, list[str]] | None = None,
    warning_map: dict[str, list[dict]] | None = None,
) -> str:
    """Carry supplied discovery coordinates as untrusted data alongside the evidence request."""
    alarm = scoping_result.raw_alarm
    parent_context = _build_parent_context(
        hypothesis, hypotheses_by_id, evidence_map, fact_map, source_ref_map, warning_map
    )

    return EVIDENCE_COLLECTION_USER_PROMPT_TEMPLATE.format(
        alarm_name=alarm.alarm_name if alarm else "N/A",
        alarm_description=render_alarm_description(alarm),
        alarm_region=alarm.region if alarm else "us-east-1",
        service_name=alarm.service_name if alarm else "N/A",
        resource_id=alarm.resource_id if alarm else "N/A",
        state_change_time=alarm.state_change_time if alarm else "N/A",
        blast_radius=scoping_result.blast_radius,
        initial_severity=scoping_result.initial_severity,
        metric_observations=render_observations(scoping_result.metric_observations),
        concurrent_alarms=render_concurrent_alarms(scoping_result.concurrent_alarms),
        parent_context=parent_context,
        hypothesis_description=hypothesis.description,
        hypothesis_category=hypothesis.category,
        required_evidence="\n".join(f"- {e}" for e in hypothesis.required_evidence) or "N/A",
    ) + render_critical_facts(scoping_result)


def _is_transient(exc: Exception) -> bool:
    """Retry transport/throttle failures, never authorization or malformed input."""
    from botocore.exceptions import ClientError, ConnectionError, HTTPClientError
    from strands.types.exceptions import ModelThrottledException

    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") in {
            "ThrottlingException",
            "TooManyRequestsException",
            "ServiceUnavailableException",
            "InternalServerException",
            "RequestTimeout",
        }
    return isinstance(exc, (TimeoutError, ConnectionError, HTTPClientError, ModelThrottledException))


def _partial_tool_outputs(agent) -> str:
    """Archive already received tool results as diagnostics, excluding the model's prose."""
    messages = getattr(agent, "messages", [])
    results = [
        block["toolResult"]
        for message in messages
        if isinstance(message, dict)
        for block in message.get("content", [])
        if isinstance(block, dict) and "toolResult" in block
    ]
    for provider in getattr(agent, "_rca_read_tools", []):
        results.extend(provider.results)
    return json.dumps(results, ensure_ascii=False, default=str) if results else ""


def _tool_receipts(agent) -> list[dict]:
    """Read only transport-owned request/response receipts, never model-authored evidence sections."""
    return [
        receipt for provider in getattr(agent, "_rca_read_tools", []) for receipt in getattr(provider, "receipts", [])
    ]


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
    timeout_seconds: float = EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
    hypotheses_by_id: dict[str, Hypothesis] | None = None,
    evidence_map: dict[str, str] | None = None,
    fact_map: dict[str, list[CriticalFact]] | None = None,
    source_ref_map: dict[str, list[str]] | None = None,
    warning_map: dict[str, list[dict]] | None = None,
    on_started=None,
) -> EvidenceCollectionResult:
    """Collect with explicit admission and retain actual received responses separately from prose."""
    user_prompt = _build_user_prompt(
        hypothesis,
        scoping_result,
        hypotheses_by_id=hypotheses_by_id,
        evidence_map=evidence_map,
        fact_map=fact_map,
        source_ref_map=source_ref_map,
        warning_map=warning_map,
    )
    logger.info(
        "Collecting evidence for hypothesis %s: %s",
        hypothesis.hypothesis_id,
        hypothesis.description[:60],
    )

    output: EvidenceOutput | None = None
    agent = None
    retryable = False
    failure_reason = ""
    try:
        if timeout_seconds <= 0:
            raise InvocationNotStartedError("no evidence budget")
        agent = create_evidence_collection_agent(
            mcp_clients=mcp_clients,
            invocation_timeout_seconds=timeout_seconds,
        )
        invocation_kwargs = {"on_started": on_started} if on_started is not None else {}
        output = invoke_agent(agent, user_prompt, EvidenceOutput, timeout_seconds, **invocation_kwargs)
        if any(provider.termination_uncertain for provider in getattr(agent, "_rca_read_tools", [])):
            output = None
    except InvocationNotStartedError as exc:
        return EvidenceCollectionResult(
            hypothesis_id=hypothesis.hypothesis_id,
            summary="",
            full_evidence="",
            invocation_not_started=True,
            failure_reason=str(exc),
        )
    except Exception as exc:
        retryable = _is_transient(exc)
        failure_reason = type(exc).__name__
        logger.exception("Evidence collection failed for %s", hypothesis.hypothesis_id)

    receipts = _tool_receipts(agent) if agent is not None else []
    warnings = received_tool_warnings(receipts)
    if output is None:
        received_partial = _partial_tool_outputs(agent) if agent is not None else ""
        return EvidenceCollectionResult(
            hypothesis_id=hypothesis.hypothesis_id,
            summary=EVIDENCE_FAILED_SENTINEL,
            full_evidence=EVIDENCE_FAILED_SENTINEL,
            failed=True,
            failure_reason=failure_reason or "tool request did not complete successfully",
            retryable=retryable,
            external_request_finished=all(
                not provider.active and not provider.termination_uncertain
                for provider in getattr(agent, "_rca_read_tools", [])
            )
            if agent is not None
            else True,
            diagnostic_partial=archive_received_outputs(json.loads(received_partial)) if received_partial else "",
            collection_warnings=warnings,
        )

    full_evidence, evidence_types = _build_full_evidence(output)
    summary = (output.combined_summary or full_evidence)[:_SUMMARY_MAX_LEN]
    receipts = _tool_receipts(agent)
    received = receipts or json.loads(_partial_tool_outputs(agent) or "[]")
    raw_tool_outputs = archive_received_outputs(received) if received else ""
    facts = derive_received_facts(receipts, scoping_result)
    source_facts_available = bool(facts or scoping_result.incident_observations.critical_facts)
    successful_source = any(
        receipt.get("request_terminated") is not False
        and isinstance(receipt.get("result"), dict)
        and receipt["result"].get("status") == "success"
        and not receipt["result"].get("isError")
        and (receipt["result"].get("content") or receipt["result"].get("structuredContent"))
        for receipt in receipts
    )
    # A finished error RPC is not positive evidence. Preserve legacy supplied-source
    # adapters, but never let error-only tool prose manufacture a usable collection.
    usable = source_facts_available or bool(evidence_types and (not receipts or successful_source))
    if source_facts_available and not evidence_types:
        full_evidence = "Source-bound observations are available in the incident and received-fact records."
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
        failed=not usable,
        failure_reason="" if usable else "no usable evidence; tool errors are collection limitations only",
        diagnostic_partial=raw_tool_outputs if not usable else "",
        critical_facts=facts,
        raw_tool_outputs=raw_tool_outputs,
        collection_warnings=warnings,
    )


def run_evidence_collection(
    hypotheses: list[Hypothesis],
    scoping_result: ScopingResult,
    *,
    mcp_clients: list[MCPClient] | None = None,
    timeout_seconds: float = EVIDENCE_COLLECTION_TIMEOUT_SECONDS,
    rca_id: str = "",
    trace=None,
    s3_client=None,
    existing_evidence_map: dict[str, str] | None = None,
    all_hypotheses: list[Hypothesis] | None = None,
    cancel_checker=None,
    save_lease=None,
    collection_states: dict[str, CollectionAttempt] | None = None,
    existing_fact_map: dict[str, list[CriticalFact]] | None = None,
    existing_source_ref_map: dict[str, list[str]] | None = None,
    existing_warning_map: dict[str, list[dict]] | None = None,
) -> EvidenceCollectionSummary:
    """Share a fixed batch budget fairly and publish only owned, finished attempts."""
    lookup_map: dict[str, str] = {}
    if existing_evidence_map:
        lookup_map.update(existing_evidence_map)
    new_evidence_map: dict[str, str] = {}
    full_evidence_map: dict[str, str] = {}
    fact_map, source_ref_map, warning_map = {}, {}, {}
    lookup_facts = dict(existing_fact_map or {})
    lookup_refs = dict(existing_source_ref_map or {})
    lookup_warnings = dict(existing_warning_map or {})
    failed_ids: set[str] = set()
    source = all_hypotheses if all_hypotheses else hypotheses
    hypotheses_by_id = {h.hypothesis_id: h for h in source}
    deadline = time.monotonic() + max(0, timeout_seconds)
    states = collection_states if collection_states is not None else {}
    scope_key = evidence_scope_key(scoping_result)

    for index, h in enumerate(hypotheses):
        if cancel_checker is not None:
            cancel_checker()
        record = states.setdefault(h.hypothesis_id, CollectionAttempt(scope_key=scope_key))
        if record.scope_key != scope_key and record.external_request_finished:
            record = states[h.hypothesis_id] = CollectionAttempt(scope_key=scope_key)
            lookup_map.pop(h.hypothesis_id, None)
            lookup_facts.pop(h.hypothesis_id, None)
            lookup_refs.pop(h.hypothesis_id, None)
            lookup_warnings.pop(h.hypothesis_id, None)
        if record.scope_key != scope_key:
            continue
        if not record.eligible():
            continue
        remaining_seconds = max(0.0, deadline - time.monotonic())
        if remaining_seconds <= 0:
            record.reason = "batch budget exhausted before invocation"
            continue
        budget_seconds = remaining_seconds / (len(hypotheses) - index)
        attempt_number = record.attempts + 1
        admitted = False

        def begin_attempt(record=record, attempt_number=attempt_number, budget_seconds=budget_seconds):
            """Record RUNNING and consume an attempt only after invocation admission."""
            nonlocal admitted
            if not admitted:
                record.status = CollectionStatus.RUNNING
                record.attempts = attempt_number
                record.started_at = datetime.now(UTC).isoformat()
                record.ended_at = None
                record.budget_seconds = budget_seconds
                record.external_request_finished = False
                admitted = True

        result = collect_evidence(
            h,
            scoping_result,
            mcp_clients=mcp_clients,
            timeout_seconds=budget_seconds,
            hypotheses_by_id=hypotheses_by_id,
            evidence_map=lookup_map,
            fact_map=lookup_facts,
            source_ref_map=lookup_refs,
            warning_map=lookup_warnings,
            on_started=begin_attempt,
        )

        if cancel_checker is not None:
            cancel_checker()
        if states[h.hypothesis_id] is not record:
            continue
        if result.invocation_not_started:
            record.reason = result.failure_reason
            record.budget_seconds = budget_seconds
            continue
        # Synchronous collection adapters/test doubles may not expose admission;
        # their returned result still establishes that a call actually ran.
        if not admitted:
            begin_attempt()
        if record.attempts != attempt_number:
            continue
        if not result.external_request_finished:
            result.failed = True
        record.ended_at = datetime.now(UTC).isoformat()
        record.external_request_finished = result.external_request_finished
        record.retryable = result.retryable
        record.status = CollectionStatus.FAILED if result.failed else CollectionStatus.COMPLETE
        record.reason = (result.failure_reason or result.summary) if result.failed else ""
        lookup_map[h.hypothesis_id] = result.summary
        new_evidence_map[h.hypothesis_id] = result.summary
        fact_map[h.hypothesis_id] = [] if result.failed else result.critical_facts
        source_ref_map[h.hypothesis_id] = []
        warning_map[h.hypothesis_id] = result.collection_warnings

        if result.failed:
            failed_ids.add(h.hypothesis_id)

        if rca_id and result.diagnostic_partial:

            def save_diagnostic(h=h, record=record, result=result):
                return _save_single_evidence_to_s3(
                    rca_id,
                    f"{h.hypothesis_id}/attempt-{record.attempts}-diagnostic",
                    result.diagnostic_partial,
                    s3_client=s3_client,
                )

            if save_lease is None:
                diagnostic_key = save_diagnostic()
            else:
                with save_lease(f"evidence-diagnostic:{h.hypothesis_id}"):
                    diagnostic_key = save_diagnostic()
            if diagnostic_key:
                source_ref_map[h.hypothesis_id].append(f"s3://{S3_EVIDENCE_BUCKET}/{diagnostic_key}")

        if rca_id and not result.failed:

            def save_completed(h=h, record=record, result=result):
                """Use one owned write with separate original and model-note fields.

                A single write preserves the existing side-effect lease budget.
                Scope and attempt identify the original response archive so a
                later successful collection cannot replace an earlier source.
                """
                if result.raw_tool_outputs:
                    key = _save_single_evidence_to_s3(
                        rca_id,
                        f"{h.hypothesis_id}/scope-{record.scope_key}/attempt-{record.attempts}-tool-results",
                        json.dumps(
                            {"tool_results": json.loads(result.raw_tool_outputs), "model_notes": result.full_evidence},
                            ensure_ascii=False,
                        ),
                        s3_client=s3_client,
                    )
                    if key:
                        source_ref_map[h.hypothesis_id].append(f"s3://{S3_EVIDENCE_BUCKET}/{key}")
                else:
                    _save_single_evidence_to_s3(rca_id, h.hypothesis_id, result.full_evidence, s3_client=s3_client)

            if save_lease is None:
                save_completed()
            else:
                with save_lease(f"evidence:{h.hypothesis_id}"):
                    save_completed()
        if trace:
            trace.update_hypothesis_evidence(
                h.hypothesis_id,
                evidence_summary=result.summary,
                critical_facts=[fact.model_dump(mode="json") for fact in fact_map[h.hypothesis_id]],
                evidence_refs=source_ref_map[h.hypothesis_id],
            )
        lookup_facts[h.hypothesis_id] = fact_map[h.hypothesis_id]
        lookup_refs[h.hypothesis_id] = source_ref_map[h.hypothesis_id]
        lookup_warnings[h.hypothesis_id] = warning_map[h.hypothesis_id]

    return EvidenceCollectionSummary(
        evidence_map=new_evidence_map,
        full_evidence_map=full_evidence_map,
        failed_ids=failed_ids,
        collection_states=states,
        fact_map=fact_map,
        source_ref_map=source_ref_map,
        warning_map=warning_map,
    )


def _save_single_evidence_to_s3(
    rca_id: str,
    hypothesis_id: str,
    evidence_text: str,
    *,
    s3_client=None,
    max_retries: int = S3_EVIDENCE_MAX_RETRIES,
    base_delay: float = EVIDENCE_SAVE_BASE_DELAY_SECONDS,
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
    base_delay: float = EVIDENCE_SAVE_BASE_DELAY_SECONDS,
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
