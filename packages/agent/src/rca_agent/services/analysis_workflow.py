"""Sequence private analysis parts without waiting for runbook approval or execution."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime

from rca_agent.adapters.secondary.report.s3_report_store import _render_markdown
from rca_agent.ports.dto.models import AlarmPayload, Hypothesis, RcaReport, RcaSessionState, ScopingResult
from rca_agent.ports.dto.observations import IncidentObservations
from rca_agent.services.analysis_parts import approval_digest
from rca_agent.services.analysis_roles import (
    generate_code_preview,
    generate_operations,
    recovery_result,
    unavailable_code,
)
from rca_agent.services.deployment_baseline import build_rollback_context
from rca_agent.services.recovery_evidence import prepare_recovery_evidence
from rca_agent.services.report import run_report_generation
from rca_agent.utils.agent_invocation import (
    InvocationNotStartedError,
    InvocationStoppedError,
    WorkAdmissionError,
    require_request_budget,
)

logger = logging.getLogger(__name__)


def _control(orchestrator, run) -> None:
    """Keep explicit cancellation and claim ownership effective between every role and publication."""
    orchestrator._check_shutdown()
    run.trace.check_cancelled()


def _run_part(orchestrator, store, run, part, work) -> dict:
    """Reuse durable results, and publish explicit failures before admitting the next logical role."""
    _control(orchestrator, run)
    previous = store.read_part(run.rca_id, part)
    if previous and previous["payload"] is not None:
        return previous
    status, error, result = "COMPLETED", "", {}
    try:
        require_request_budget()
    except (WorkAdmissionError, InvocationNotStartedError) as exc:
        status, error = "SKIPPED", type(exc).__name__
    else:
        store.start_part(run.rca_id, part, run.claim_token, run.attempt)
        try:
            result = work()
        except InvocationStoppedError:
            raise
        except Exception as exc:
            _control(orchestrator, run)
            logger.warning("Analysis part %s failed; exception_type=%s", part, type(exc).__name__)
            status, error = "FAILED", type(exc).__name__
    _control(orchestrator, run)
    ready = part == "recovery" and result.get("recommendation") == "ROLLBACK" and status == "COMPLETED"
    prior_name = {"root_cause": "recovery", "operations": "root_cause"}.get(part)
    prior = store.read_part(run.rca_id, prior_name) if prior_name else None
    input_refs = (
        [{"part": prior_name, "key": prior["record"]["payload_s3_key"], "sha256": prior["record"]["payload_sha256"]}]
        if prior
        else []
    )
    return store.publish_part(
        run.rca_id,
        part,
        run.claim_token,
        run.attempt,
        result=result,
        status=status,
        error=error,
        input_refs=input_refs,
        approval_status="READY" if ready else "UNAVAILABLE",
        runbook_digest=approval_digest(result["playbook"]) if ready else "",
    )


def hypothesis_path(selected, hypotheses) -> list[str]:
    """Preserve the actual parent chain without inventing missing ancestors or crossing trees."""
    by_id = {item.hypothesis_id: item for item in hypotheses}
    path, seen = [], set()
    current = selected
    while current is not None:
        if current.hypothesis_id in seen:
            raise ValueError("hypothesis parent cycle")
        seen.add(current.hypothesis_id)
        path.append(current.description)
        if not current.parent_id:
            break
        parent = by_id.get(current.parent_id)
        if parent is None or parent.tree_id != current.tree_id:
            raise ValueError("hypothesis ancestor missing from the selected tree")
        current = parent
    return list(reversed(path))


def _root_work(orchestrator, alarm, scoping, incident, run) -> dict:
    """Keep the existing tree and thresholds inside root_cause, using only the pinned incident evidence."""
    container = orchestrator._container
    hypotheses = orchestrator._run_hypothesis_generation(scoping, run)
    if not hypotheses:
        raise ValueError("No hypotheses generated")
    from rca_agent.services.frozen_evidence import frozen_evidence_scope

    with frozen_evidence_scope(scoping):
        state = orchestrator._run_validation_loop(alarm, scoping, hypotheses, run)
    selected, confirmed = orchestrator._finalize_hypotheses(
        state.hypotheses, state.termination, state.all_judgments, trace=run.trace
    )
    _control(orchestrator, run)
    container.session_store.update_state(run.rca_id, RcaSessionState.REPORT_GENERATION, claim_token=run.claim_token)
    from rca_agent.adapters.secondary.trace.dynamodb_trace_store import SpanType

    with run.trace.span(SpanType.REPORT, input_summary="root_cause report") as span:
        report = run_report_generation(
            scoping,
            selected,
            confirmed,
            hypothesis_path(selected, state.hypotheses),
            [state.model_evidence(key) for key in state.evidence_map if state.model_evidence(key)],
            state.rejected_descriptions,
            state.timeline,
            container.report_agent,
        )
        span.output_summary = f"rca_id={run.rca_id}, confidence={report.confidence_score}"
    report.rca_id = run.rca_id
    try:
        preview = generate_code_preview(
            report,
            {
                **incident,
                "source_artifacts": [*incident.get("source_artifacts", []), *getattr(state, "source_artifacts", [])],
            },
            container.code_preview_agent,
        )
    except InvocationStoppedError:
        raise
    except Exception as exc:
        _control(orchestrator, run)
        preview = unavailable_code(type(exc).__name__)
    return {
        "title": "근본 원인 및 코드 개선",
        "summary": report.incident_summary,
        "root_cause": {
            "description": report.root_cause,
            "confirmed": report.root_cause_confirmed,
            "confidence": report.confidence_score,
            "selected_hypothesis_id": report.selected_hypothesis_id,
        },
        "report_markdown": _render_markdown(report, None),
        "report": report.model_dump(mode="json"),
        "selected_hypothesis": selected.model_dump(mode="json") if selected else None,
        "validated_fault_type": selected.validated_fault_type.value if selected else "UNSUPPORTED",
        "source_artifacts": getattr(state, "source_artifacts", []),
        "control_artifacts": getattr(state, "control_artifacts", []),
        "code_proposal": preview,
    }


def run_analysis_parts(orchestrator, alarm, run, store) -> bool:
    """Freeze before recovery publication, preserve private revisions, and complete globally only at the end."""
    container = orchestrator._container
    frozen = store.read_incident(run.rca_id)
    resumed = frozen is not None
    if frozen is None:
        scoping = orchestrator._run_scoping(alarm, run)
        _control(orchestrator, run)
        prepared = prepare_recovery_evidence(scoping)
        observations = scoping.incident_observations.model_dump(mode="json")
        observations["recovery_evidence"] = prepared
        observations["incident_cutoff"] = datetime.now(UTC).isoformat()
        sources = getattr(container, "source_artifacts", [])
        raw_alarm = deepcopy(run.alarm_data) if run.alarm_data is not None else alarm.model_dump(mode="json")
        normalized = (
            AlarmPayload.from_cloudwatch_sns(raw_alarm)
            if "AlarmName" in raw_alarm
            else AlarmPayload.model_validate(raw_alarm)
        )
        if any(
            getattr(normalized, field) != getattr(alarm, field)
            for field in ("alarm_name", "alarm_arn", "region", "state_change_time", "alarm_description")
        ):
            raise ValueError("original alarm identity differs from the scoped incident")
        incident = {
            "alarm": raw_alarm,
            "scoping": scoping.model_dump(mode="json"),
            "observations": observations,
            "source_artifacts": sources if isinstance(sources, list) else [],
        }
        frozen = store.freeze_incident(run.rca_id, run.claim_token, run.attempt, incident)
    else:
        source = frozen["payload"]
        pinned_alarm = source["alarm"]
        alarm = (
            AlarmPayload.from_cloudwatch_sns(pinned_alarm)
            if "AlarmName" in pinned_alarm
            else AlarmPayload.model_validate(pinned_alarm)
        )
        scoped = dict(source.get("scoping") or {})
        scoped.setdefault("alarm_summary", alarm.new_state_reason or alarm.alarm_name)
        scoped["raw_alarm"] = alarm
        scoped["incident_observations"] = IncidentObservations.model_validate(source["observations"])
        scoping = ScopingResult.model_validate(scoped)
        # Reclaim resets the inner state; resuming the frozen scope must not perform new observations.
        container.session_store.update_state(run.rca_id, RcaSessionState.SCOPING, claim_token=run.claim_token)
    incident = frozen["payload"]
    object.__setattr__(
        scoping, "_frozen_cutoff", incident["observations"].get("incident_cutoff", frozen["record"]["created_at"])
    )
    prepared = incident["observations"].get("recovery_evidence", {})
    verification = {
        **prepared.get("verification", {}),
        "valid": prepared.get("verification", {}).get("status") == "VERIFIED",
        "rollback_context": prepared.get("context"),
    }

    def recovery():
        """Supply only the reader-verified eligibility to the early-only plan validator."""
        _control(orchestrator, run)
        observer = getattr(container, "incident_observer", None)
        if observer is None:
            return recovery_result(
                run.rca_id,
                scoping,
                {**verification, "valid": False, "reason": "current control refresh unavailable"},
                container.recovery_agent,
            )
        require_request_budget()
        current_scope = scoping.model_copy(deep=True)
        current_scope.incident_observations = observer.refresh_current(
            alarm, scoping.incident_observations, timeout_seconds=300
        )
        _control(orchestrator, run)
        fresh = prepare_recovery_evidence(current_scope)
        current_verification = {
            **fresh["verification"],
            "valid": fresh["verification"]["status"] == "VERIFIED",
            "rollback_context": fresh["context"],
        }
        # A verified immutable snapshot can survive a process restart; its private
        # reader receipt cannot. Retain that existing proof only for the exact
        # same context after the live control guard passes again.
        if resumed and verification["valid"]:
            if build_rollback_context(current_scope) == verification["rollback_context"]:
                current_verification = dict(verification)
            else:
                current_verification.update(valid=False, reason="current deployment no longer matches frozen recovery")
        current_verification["current_control"] = deepcopy(
            {
                key: value
                for key, value in current_scope.incident_observations.current.items()
                if key not in {"observations", "log_window"}
            }
        )
        current_verification["control_diagnostics"] = list(current_scope.incident_observations.diagnostics)
        return recovery_result(run.rca_id, current_scope, current_verification, container.recovery_agent)

    def root():
        """Use the same existing root algorithm after the recovery outcome is durable."""
        return _root_work(orchestrator, alarm, scoping, incident, run)

    recovery_part = _run_part(orchestrator, store, run, "recovery", recovery)
    root_part = _run_part(orchestrator, store, run, "root_cause", root)

    def operations():
        """A root failure is still input; no execution state is consulted before prevention work."""
        return generate_operations(incident, root_part["payload"], container.operations_agent)

    operations_part = _run_part(orchestrator, store, run, "operations", operations)
    _control(orchestrator, run)
    result = root_part["payload"].get("result", {})
    cause = result.get("root_cause", {})
    report = (
        RcaReport.model_validate(result["report"])
        if result.get("report")
        else RcaReport(
            rca_id=run.rca_id,
            incident_summary=result.get("summary", scoping.alarm_summary),
            severity=scoping.initial_severity,
            root_cause=cause.get("description", "Unknown"),
            root_cause_confirmed=cause.get("confirmed", False),
            confidence_score=cause.get("confidence", 0),
            selected_hypothesis_id=cause.get("selected_hypothesis_id", ""),
            incident_observations=scoping.incident_observations.model_copy(deep=True),
        )
    )
    if report.rca_id != run.rca_id:
        raise ValueError("root report snapshot belongs to another incident")
    public_fields = {
        "schema_version",
        "workflow",
        "rca_id",
        "engine",
        "part",
        "status",
        "revision",
        "payload_s3_key",
        "payload_sha256",
        "incident_s3_key",
        "incident_sha256",
        "approval_status",
        "runbook_digest",
        "summary",
        "error",
        "created_at",
        "completed_at",
    }
    report.analysis_parts = {
        part["record"]["part"]: {
            "record": {key: value for key, value in part["record"].items() if key in public_fields},
            "payload": part["payload"],
        }
        for part in (recovery_part, root_part, operations_part)
    }
    report.analysis_part_refs = {
        name: {key: value["record"][key] for key in ("engine", "part", "revision", "payload_s3_key", "payload_sha256")}
        for name, value in report.analysis_parts.items()
    }
    selected = Hypothesis.model_validate(result["selected_hypothesis"]) if result.get("selected_hypothesis") else None
    return orchestrator._persist_report_and_notify(
        report, scoping, run, best_hypothesis=selected, alarm=alarm, parts_store=store
    )
