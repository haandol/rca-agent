"""Check source and rollback constraints without a model or external service."""

import hashlib
import json
import subprocess
from unittest.mock import Mock

import pytest

from rca_agent.ports.dto.models import ScopingResult
from rca_agent.services import analysis_roles
from rca_agent.services.analysis_parts import approval_digest
from rca_agent.services.analysis_roles import (
    CodeFile,
    CodePreview,
    recovery_result,
    validate_code_preview,
)


def source():
    """Supply exact actually-read bytes and a snapshot identity, not an invented Git revision."""
    text = "first\nsecond\nthird\n"
    return {
        "path": "app.py",
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_ref": "read:1",
        "base_revision": "snapshot:1",
        "source_phase": "current",
    }


def preview():
    """Build a verifiable one-line proposal whose diff must come from the server."""
    return CodePreview(
        status="PROPOSED",
        title="fix",
        base_revision="snapshot:1",
        files=[
            CodeFile(
                path="app.py",
                start_line=2,
                end_line=2,
                original="second\n",
                proposed="replacement\n",
                evidence_refs=["read:1"],
            )
        ],
    )


def test_exact_source_preview_is_rendered_without_claiming_tests_ran():
    """Source equality yields a reviewable diff and NOT_RUN, never a published PR."""
    output = validate_code_preview(preview(), [source()])
    assert "-second\n+replacement\n" in output["files"][0]["unified_diff"]
    assert output["tests_status"] == "NOT_RUN"


@pytest.mark.parametrize(
    "field,value",
    [
        ("original", "invented"),
        ("start_line", 1),
        ("end_line", 20),
        ("path", "not-read.py"),
        ("evidence_refs", ["unknown"]),
    ],
)
def test_preview_refuses_unread_source_or_wrong_range(field, value):
    """A plausible patch cannot become PROPOSED when original bytes or provenance differ."""
    item = preview()
    setattr(item.files[0], field, value)
    with pytest.raises(ValueError):
        validate_code_preview(item, [source()])


def test_missing_recovery_compatibility_returns_unavailable_without_model(monkeypatch):
    """Early recovery does not bypass missing server input evidence even before root confirmation."""
    invoke = Mock(side_effect=AssertionError("must not call model"))
    monkeypatch.setattr(analysis_roles, "invoke_agent", invoke)
    result = recovery_result("rca", ScopingResult(alarm_summary="incident"), {"valid": False}, object())
    assert result["recommendation"] == "UNAVAILABLE" and result["playbook"] is None
    invoke.assert_not_called()


def test_digest_matches_existing_dashboard_approval_serialization():
    """The payload hash and complete playbook approval digest use their own exact representations."""
    value = {"z": [1.0, 1e-7, 1e-6, -0.0, "한글"], "a": {"𐀀": 1, "\ue000": 2}, "b": None}
    script = """
const crypto=require('crypto');
const x=JSON.parse(process.argv[1]);

function c(v){return Array.isArray(v)?v.map(c):v&&typeof v==='object'
?Object.fromEntries(Object.keys(v).sort().map(k=>[k,c(v[k])])):v};
process.stdout.write(crypto.createHash('sha256').update(JSON.stringify(c(x))).digest('hex'))
"""
    result = subprocess.check_output(["node", "-e", script, json.dumps(value)], text=True)
    assert approval_digest(value) == result


@pytest.mark.usefixtures("compatible")
def test_early_rollback_uses_real_reader_verification_and_keeps_legacy_root_guard(compatible, monkeypatch):
    """A complete early plan is valid without changing the old unconfirmed-RCA empty-step rule."""
    from rca_agent.services.playbook_gen import ExecutionStepOutput, build_execution_steps
    from rca_agent.services.recovery_evidence import prepare_recovery_evidence
    from tests.test_deployment_baseline import observed_plan

    scoping, context, steps = observed_plan(compatible)
    steps[-1]["success_criteria"] = (
        steps[-1]["metric_wait"]["failure_alarm_name"]
        + " OK; "
        + steps[-1]["metric_wait"]["metrics"]["failures"]["metric_name"]
        + " zero; completed writes positive"
    )
    evidence = prepare_recovery_evidence(scoping)
    assert evidence["verification"]["status"] == "VERIFIED"
    verification = {**evidence["verification"], "valid": True, "rollback_context": context}

    def invoke(agent, prompt, output_model, timeout):
        """Exercise the actual dynamic output validator instead of bypassing its observed-plan checks."""
        return output_model.model_validate(
            {
                "title": "rollback",
                "summary": "restore",
                "reason": "verified baseline",
                "recommendation": "ROLLBACK",
                "playbook": {
                    "failure_type": "observed mismatch",
                    "symptom_pattern": "writes fail",
                    "execution_steps": steps,
                },
            }
        )

    monkeypatch.setattr(analysis_roles, "invoke_agent", invoke)
    result = recovery_result("rca", scoping, verification, object())
    assert result["recommendation"] == "ROLLBACK"
    assert result["playbook"]["rollback_context"] == context
    assert (
        build_execution_steps([ExecutionStepOutput(**step) for step in steps], confirmed=False, scoping_result=scoping)
        == []
    )
    steps[0]["ecs_service_precondition"]["expected_deployment_id"] = "foreign"
    with pytest.raises(ValueError):
        recovery_result("rca", scoping, verification, object())


pytest_plugins = ["tests.test_recovery_evidence"]


def test_operations_observed_requires_actual_control_refs_and_missing_ci_stays_unverified(monkeypatch):
    """The SDK correction boundary rejects invented observed controls, while qualified recommendations remain useful."""
    from rca_agent.services.analysis_roles import generate_operations

    raw = {
        "title": "ops",
        "summary": "checks",
        "findings": [{"statement": "CI check exists", "status": "OBSERVED", "evidence_refs": ["read:ci"]}],
        "recommendations": [],
    }

    def invoke(agent, prompt, output_model, timeout):
        """Use the actual dynamic model validator for both negative and source-qualified outputs."""
        return output_model.model_validate(raw)

    monkeypatch.setattr(analysis_roles, "invoke_agent", invoke)
    with pytest.raises(ValueError, match="actually read"):
        generate_operations({}, {"result": {}}, object())
    raw["findings"][0]["status"] = "UNVERIFIED"
    assert generate_operations({}, {"result": {}}, object())["findings"][0]["status"] == "UNVERIFIED"
    raw["findings"][0]["status"] = "OBSERVED"
    control = {**source(), "source_ref": "read:ci", "source_kind": "read_control_configuration"}
    assert (
        generate_operations({}, {"result": {"control_artifacts": [control]}}, object())["findings"][0]["status"]
        == "OBSERVED"
    )


def test_noop_baseline_and_already_fixed_target_cannot_be_proposed():
    """Do not manufacture a duplicate preview from unchanged bytes, a normal baseline, or an obsolete target."""
    noop = preview()
    noop.files[0].proposed = noop.files[0].original
    with pytest.raises(ValueError, match="does not change"):
        validate_code_preview(noop, [source()])
    with pytest.raises(ValueError, match="baseline"):
        validate_code_preview(preview(), [{**source(), "source_phase": "baseline"}])
    current = {**source(), "repository": "owner/repo", "base_ref": "snapshot:1"}
    fixed = {
        **source(),
        "source_ref": "read:fixed",
        "repository": "owner/repo",
        "source_phase": "baseline",
        "base_ref": "new-target",
        "base_revision": "new-target",
        "is_current_target": True,
    }
    with pytest.raises(ValueError, match="actually read target"):
        validate_code_preview(preview(), [current, fixed])
