"""The operations role keeps evidence and lineage without inheriting unverified recommendation prose."""

from copy import deepcopy

from rca_agent.services.operations_context import operations_context


def test_role_projection_preserves_authority_and_excludes_duplicate_unverified_recommendations():
    """Earlier migration recommendations are not facts; original objects and immutable references remain."""
    incident = {
        "rca_id": "rca",
        "engine": "strands",
        "alarm": {"AlarmName": "alarm", "unknown_source_metadata": "keep"},
        "observations": {
            "incident_cutoff": "2026-09-17T00:00:00Z",
            "baseline_verified": True,
            "critical_facts": [{"sqlstate": "42703", "source_ref": "log:measured"}],
            "baseline": {"baseline_ref": {"key": "baseline"}, "observations": [{"raw": "repeated"}]},
            "current": {"task_definition_arn": "td", "service_settings": {"desiredCount": 1}},
        },
    }
    root = {
        "rca_id": "rca",
        "part": "root_cause",
        "status": "COMPLETED",
        "incident_ref": {"key": "immutable-original", "sha256": "digest"},
        "input_refs": [{"part": "recovery", "key": "early"}],
        "result": {
            "root_cause": {"description": "bad SQL", "confirmed": True, "confidence": 0.95},
            "code_proposal": {"status": "UNAVAILABLE", "limitations": ["source unavailable"]},
            "report_markdown": "UNPROVEN_MIGRATION " * 1000,
            "report": {
                "permanent_remediation": "UNPROVEN_MIGRATION",
                "five_whys": ["UNPROVEN_MIGRATION"],
                "evidence_list": ["bounded evidence log:measured"],
            },
        },
    }
    before = deepcopy((incident, root))
    result = operations_context(incident, root)
    assert result["incident"]["alarm"] == incident["alarm"]
    assert result["incident"]["critical_facts"] == incident["observations"]["critical_facts"]
    assert result["incident"]["current"] == incident["observations"]["current"]
    assert result["incident"]["baseline"]["baseline_ref"] == {"key": "baseline"}
    assert result["root_result"]["incident_ref"] == root["incident_ref"]
    assert result["root_result"]["input_refs"] == root["input_refs"]
    assert result["root_result"]["root_cause"] == root["result"]["root_cause"]
    assert result["root_result"]["code_proposal"] == root["result"]["code_proposal"]
    assert result["root_result"]["evidence_summaries"] == ["bounded evidence log:measured"]
    assert "UNPROVEN_MIGRATION" not in str(result)
    assert (incident, root) == before


def test_failed_root_retains_status_error_and_lineage_without_inventing_confirmation():
    """A failed prior role still reaches operations as a failure, with its original incident reference."""
    root = {"status": "FAILED", "error": "ProviderError", "incident_ref": {"key": "original"}, "result": {}}
    result = operations_context({}, root)["root_result"]
    assert result["status"] == "FAILED" and result["error"] == "ProviderError"
    assert result["incident_ref"] == {"key": "original"}
    assert result["root_cause"] == {}
