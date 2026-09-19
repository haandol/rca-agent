"""Model-only public knowledge inputs; immutable report/part/approval documents remain untouched."""

from copy import deepcopy

from rca_agent.ports.dto.models import ScopingResult
from rca_agent.services.recovery_context import _digest, recovery_context


def incident_reference(report) -> dict | None:
    """Use the existing immutable part lineage, never invent an archive location."""
    for part in ("root_cause", "recovery", "operations"):
        reference = report.analysis_parts.get(part, {}).get("payload", {}).get("incident_ref")
        if isinstance(reference, dict):
            return deepcopy(reference)
    return None


def comparison_decision_context(report) -> dict:
    """Label the supplied final selection separately from retained earlier evidence commentary."""
    return {
        "final_selected_root": {
            "stage": "FINAL_REPORT_SELECTED_ROOT",
            "rca_id": report.rca_id,
            "hypothesis_id": report.selected_hypothesis_id,
            "hypothesis_title": report.selected_hypothesis_title,
            "root_cause": report.root_cause,
            "confirmed": report.root_cause_confirmed,
            "confidence_score": report.confidence_score,
            "source_part_ref": deepcopy(report.analysis_part_refs.get("root_cause")),
        },
        "retained_commentary": {
            "source_path": "current_report#/evidence_list",
            "stage": "COLLECTION_AND_VALIDATION_COMMENTARY",
            "meaning": (
                "Entries are preserved verbatim and may contain earlier or unselected hypothesis commentary. "
                "An entry without explicit selection metadata is not a replacement final decision."
            ),
        },
        "comparison_role": (
            "Reuse the supplied final selected root state; this comparison does not rerun root validation. "
            "Not independently revalidated here is different from an unknown or unconfirmed cause. "
            "Preserve confirmed=false when supplied; never promote it. Preserve confirmed=true when supplied; "
            "do not downgrade the selected technical mechanism using earlier commentary. "
            "Unproved upstream intent, process omission, impact and execution outcome remain separate limitations."
        ),
    }


def public_observations(scoping, reference=None) -> dict:
    """Retain control/metrics/facts/descriptors while replacing repeated raw events with bound summaries."""
    verification = {"incident_ref": reference} if reference else {}
    projected = recovery_context(scoping, verification)["scoping"]
    projected.pop("similar_reports", None)
    return projected


def public_part_proposals(report) -> dict:
    """Keep all completed proposal content and source metadata without recopying raw receipt/file archives."""
    outputs = report.analysis_parts
    operations = outputs.get("operations", {}).get("payload", {}).get("result", {})
    projected = {
        key: deepcopy(value)
        for key, value in operations.items()
        if key not in {"repository_collection", "control_artifacts"}
    }
    controls = operations.get("control_artifacts", [])
    projected["control_sources"] = [
        {key: deepcopy(value) for key, value in source.items() if key != "text"} for source in controls
    ]
    collection = operations.get("repository_collection", {})
    projected["collection_warnings"] = deepcopy(collection.get("warnings", []))
    projected["original_evidence"] = {
        "part_ref": deepcopy(report.analysis_part_refs.get("operations")),
        "result_sha256": _digest(operations),
        "receipt_count": len(collection.get("receipts", [])),
        "control_source_count": len(controls),
        "note": "Full source text and receipts remain in the immutable operations part; proposals are NOT_RUN.",
    }
    return {
        "code_proposal": deepcopy(
            outputs.get("root_cause", {}).get("payload", {}).get("result", {}).get("code_proposal", {})
        ),
        "operations": projected,
        "source_part_refs": deepcopy(report.analysis_part_refs),
        "recovery_assessment": recovery_assessment(report),
    }


def recovery_assessment(report) -> dict:
    """Describe retained server recovery authority without claiming approval, execution or current eligibility."""
    part = report.analysis_parts.get("recovery", {})
    record, payload = part.get("record", {}), part.get("payload", {})
    result = payload.get("result", {})
    verification = result.get("verification", {})
    book = result.get("playbook")
    digest = record.get("payload_sha256")
    engine = payload.get("engine")
    verified = (
        record.get("status") == payload.get("status") == "COMPLETED"
        and record.get("approval_status") == "READY"
        and engine in {"strands", "headless-codex"}
        and payload.get("rca_id") == report.rca_id
        and payload.get("part") == "recovery"
        and digest == _digest(payload)
        and record.get("payload_s3_key") == f"analysis-parts/{engine}/{report.rca_id}/recovery/{digest}.json"
        and result.get("recommendation") == "ROLLBACK"
        and verification.get("valid") is True
        and isinstance(book, dict)
        and book.get("rca_id") == report.rca_id
        and isinstance(book.get("rollback_context"), dict)
        and bool(book.get("rollback_context"))
        and verification.get("rollback_context") == book["rollback_context"]
    )
    summary = {
        "status": "VERIFIED_READY" if verified else "NOT_VERIFIED",
        "part_status": record.get("status"),
        "source_part_ref": {
            "key": record.get("payload_s3_key"),
            "sha256": digest,
        },
        "meaning": (
            "Retained recovery assessment, not current live control or execution evidence. "
            "READY means a server-validated plan was prepared, not that it was approved, executed or resolved."
        ),
    }
    if verified:
        summary.update(
            rollback_context=deepcopy(book["rollback_context"]),
            control_observed_at=verification.get("current_control", {}).get("observed_at"),
            evidence_refs=deepcopy(result.get("evidence_refs", [])),
            limitations=deepcopy(result.get("limitations", [])),
        )
    return summary


def comparison_report(report) -> dict:
    """Archive compared report fields and bound part proposals instead of copying unused raw part bodies."""
    result = report.model_dump(mode="json")
    if not report.analysis_parts or any(not report.analysis_part_refs.get(part) for part in report.analysis_parts):
        return result
    original_hash = _digest(result)
    result["incident_observations"] = public_observations(
        ScopingResult(alarm_summary="", incident_observations=report.incident_observations),
        incident_reference(report),
    )["incident_observations"]
    result["part_proposals"] = public_part_proposals(report)
    result["original_source"] = {
        "report_sha256": original_hash,
        "source_part_refs": deepcopy(report.analysis_part_refs),
        "incident_ref": incident_reference(report),
        "note": "Compared report fields are retained here; original role payloads remain at immutable part references.",
    }
    return result
