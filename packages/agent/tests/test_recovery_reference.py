"""Validate reference-only guidance without changing the model's output authority."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from rca_agent.ports.dto.models import ScopingResult
from rca_agent.services import analysis_roles
from rca_agent.services.recovery_evidence import prepare_recovery_evidence
from rca_agent.services.recovery_reference import build_recovery_reference

pytest_plugins = ["tests.test_recovery_evidence"]


@pytest.fixture
def reference_scope(compatible):
    """Add an actual producer accounting observation to the reader's normal fixture."""
    reader, alarm, baseline, publish, *_ = compatible
    accounting = deepcopy(baseline["observations"][0])
    accounting["event_id"] = "normal-accounting"
    accounting["message"].update(
        event="write_accounting",
        metric_namespace=baseline["metrics"]["attempts"]["namespace"],
        service_name=baseline["metrics"]["attempts"]["dimensions"]["ServiceName"],
        attempt_metric="attempts",
        failure_metric="failures",
        attempt_semantics="completed_successful_rows_plus_failed_rows",
        failure_semantics="failed_rows",
        cancellation_semantics="excluded_from_completed_counters",
        success_evidence_event="write_completed",
        success_count_field="count",
        success_semantics="committed_rows",
    )
    baseline["observations"].append(accounting)
    publish()
    scope = ScopingResult(
        alarm_summary="fixture",
        raw_alarm=alarm,
        incident_observations=reader.observe(alarm, timeout_seconds=90),
    )
    prepared = prepare_recovery_evidence(scope)
    assert prepared["verification"]["status"] == "VERIFIED"
    return scope, prepared, {**prepared["verification"], "valid": True, "rollback_context": prepared["context"]}


def test_reference_matches_native_builder_steps_and_never_mutates_inputs(reference_scope, monkeypatch):
    """The portable Agent example retains the existing native construction and runtime acceptance."""
    scope, prepared, verification = reference_scope
    before = deepcopy((scope.model_dump(mode="json"), prepared, verification))
    source = Path(__file__).resolve().parents[2] / "headless-codex" / "src"
    monkeypatch.syspath_prepend(str(source))
    from headless_codex.services.execution_contract import validate_steps
    from headless_codex.services.recovery_plan import build_recovery_plan

    native = build_recovery_plan("fixture", {"AlarmName": scope.raw_alarm.alarm_name}, prepared)
    assert native["approval_status"] == "READY"
    reference = build_recovery_reference(scope, verification)
    native_steps = deepcopy(native["playbook"]["execution_steps"])
    # Agent guidance names only fields returned by this step; construction of
    # every operation and all native image/health guards retains native parity.
    native_steps[0]["success_criteria"] = reference["execution_steps"][0]["success_criteria"]
    assert reference["execution_steps"] == native_steps
    assert len(validate_steps({**reference, "rollback_context": prepared["context"]})) == 5
    assert set(reference) == {"execution_steps"}
    assert (scope.model_dump(mode="json"), prepared, verification) == before


def test_observe_criteria_are_service_response_fields_and_native_image_guards_remain(reference_scope):
    """The initial read must not require container fields available only in native task checks."""
    from rca_agent.prompts.analysis_parts import RECOVERY_SYSTEM_PROMPT

    scope, _, verification = reference_scope
    steps = build_recovery_reference(scope, verification)["execution_steps"]
    observe = steps[0]
    assert len(observe["commands"]) == 1 and "ecs describe-services" in observe["commands"][0]
    for field in ("serviceArn", "clusterArn", "taskDefinition", "deployments[].id"):
        assert field in observe["success_criteria"]
        assert field in RECOVERY_SYSTEM_PROMPT
    assert "imageDigest" not in observe["success_criteria"]
    assert "health" not in observe["success_criteria"].lower()
    assert "never require either in observe" in RECOVERY_SYSTEM_PROMPT
    assert "native rollback precondition and deployment" in RECOVERY_SYSTEM_PROMPT
    assert (
        steps[1]["ecs_service_precondition"]["expected_image_digest"]
        == verification["rollback_context"]["current"]["image_digest"]
    )
    assert steps[2]["deployment_wait"]["image_digest"] == verification["rollback_context"]["normal"]["image_digest"]


@pytest.mark.parametrize("change", ["missing_proof", "missing_metrics", "settings", "unverified"])
def test_reference_never_guesses_missing_or_changed_coordinates(reference_scope, change):
    """Reference construction cannot synthesize a descriptor, metric identity or replacement target."""
    scope, _, verification = reference_scope
    if change == "missing_proof":
        del verification["rollback_context"]["write_accounting"]
    elif change == "missing_metrics":
        del scope.incident_observations.baseline["metrics"]["attempts"]
    elif change == "settings":
        verification["rollback_context"]["service_settings"]["desiredCount"] = 2
    else:
        verification["valid"] = False
    with pytest.raises((ValueError, KeyError)):
        build_recovery_reference(scope, verification)


@pytest.mark.parametrize("outcome", ["accept", "decline", "empty", "failure", "drift"])
def test_reference_is_input_only_and_model_still_owns_full_result(reference_scope, monkeypatch, outcome):
    """No compiler fallback replaces an omitted, rejected or failed model result."""
    scope, _, verification = reference_scope
    captured = []

    def invoke(agent, prompt, model, timeout):
        """Inspect actual model input then exercise the unchanged output validator."""
        payload = json.loads(prompt)
        captured.append(payload)
        assert payload["scoping"] == scope.model_dump(mode="json")
        assert payload["verification"] == verification
        reference = payload["validated_recovery_reference"]
        assert reference["authority"] == "REFERENCE_ONLY"
        if outcome == "failure":
            raise RuntimeError("model failed")
        if outcome == "decline":
            return model.model_validate(
                {"title": "model", "summary": "declined", "reason": "uncertainty", "recommendation": "UNAVAILABLE"}
            )
        steps = deepcopy(reference["execution_steps"])
        if outcome == "empty":
            steps = []
        elif outcome == "drift":
            steps[1]["ecs_service_precondition"]["expected_deployment_id"] = "foreign"
        return model.model_validate(
            {
                "title": "model-authored",
                "summary": "model summary",
                "reason": "model reason",
                "recommendation": "ROLLBACK",
                "playbook": {
                    "failure_type": "model-selected description",
                    "symptom_pattern": "model symptom",
                    "execution_steps": steps,
                },
            }
        )

    monkeypatch.setattr(analysis_roles, "invoke_agent", invoke)
    if outcome in {"empty", "drift", "failure"}:
        with pytest.raises(RuntimeError if outcome == "failure" else ValueError):
            analysis_roles.recovery_result("fixture", scope, verification, object())
    else:
        result = analysis_roles.recovery_result("fixture", scope, verification, object())
        if outcome == "decline":
            assert result["playbook"] is None
        else:
            assert result["playbook"]["failure_type"] == "model-selected description"
            assert result["title"] == "model-authored"
    assert len(captured) == 1


def test_invalid_reference_is_omitted_without_replacing_existing_model_input(reference_scope, monkeypatch):
    """A reference failure retains the original generation path and complete current evidence."""
    scope, _, verification = reference_scope
    monkeypatch.setattr(
        analysis_roles, "build_recovery_reference", Mock(return_value={"execution_steps": [{"step_id": "bad"}]})
    )

    def decline(agent, prompt, model, timeout):
        """Reject the reference before input while retaining model discretion to decline."""
        payload = json.loads(prompt)
        assert set(payload) == {"scoping", "verification"}
        return model.model_validate(
            {"title": "decline", "summary": "decline", "reason": "limited", "recommendation": "UNAVAILABLE"}
        )

    monkeypatch.setattr(analysis_roles, "invoke_agent", decline)
    assert analysis_roles.recovery_result("fixture", scope, verification, object())["playbook"] is None
