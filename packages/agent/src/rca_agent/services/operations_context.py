"""Project operational evidence without promoting earlier model recommendations to facts."""

from copy import deepcopy


def operations_context(incident: dict, root_payload: dict) -> dict:
    """Retain immutable lineage, server judgment and evidence while removing duplicated report prose."""
    result = root_payload.get("result", {})
    report = result.get("report", {})
    observations = incident.get("observations") or report.get("incident_observations", {})
    incident_context = {
        key: deepcopy(incident[key]) for key in ("schema_version", "rca_id", "engine", "alarm") if key in incident
    }
    incident_context.update(
        incident_ref=deepcopy(root_payload.get("incident_ref")),
        incident_cutoff=observations.get("incident_cutoff"),
        baseline_verified=observations.get("baseline_verified"),
        critical_facts=deepcopy(observations.get("critical_facts", [])),
        diagnostics=deepcopy(observations.get("diagnostics", [])),
        observation_storage_note="Raw observations remain in the immutable incident_ref; they are not reclassified.",
    )
    for phase in ("baseline", "current"):
        incident_context[phase] = {
            key: deepcopy(value) for key, value in observations.get(phase, {}).items() if key != "observations"
        }
    root_context = {
        key: deepcopy(root_payload[key])
        for key in (
            "schema_version",
            "workflow",
            "engine",
            "rca_id",
            "part",
            "status",
            "error",
            "incident_ref",
            "input_refs",
        )
        if key in root_payload
    }
    root_context.update(
        root_cause=deepcopy(result.get("root_cause", {})),
        validated_fault_type=result.get("validated_fault_type"),
        selected_hypothesis=deepcopy(result.get("selected_hypothesis")),
        code_proposal=deepcopy(result.get("code_proposal", {})),
        evidence_summaries=deepcopy(report.get("evidence_list", [])),
        limitations=deepcopy(root_payload.get("limitations", [])),
        collection_warnings=deepcopy(result.get("repository_collection", {}).get("warnings", [])),
    )
    return {"incident": incident_context, "root_result": root_context}
