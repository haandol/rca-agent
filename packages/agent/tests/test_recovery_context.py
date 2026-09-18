"""Model projection reduces repetition without modifying complete stored proof or approval inputs."""

import json
from copy import deepcopy
from unittest.mock import Mock

from rca_agent.services import analysis_roles, analysis_workflow
from rca_agent.services.recovery_context import _digest, recovery_context
from tests.test_analysis_workflow import wired_pipeline

pytest_plugins = ["tests.test_recovery_reference", "tests.test_analysis_parts"]


def test_projection_keeps_every_binding_and_full_server_inputs(reference_scope):
    """629 repeated witnesses become summaries; a distinct target/version group cannot disappear."""
    scope, _, verification = reference_scope
    first = verification["witnesses"][-1]
    witnesses = []
    for number in range(629):
        witnesses.append({**first, "input_ref": f"input:{number}", "outcome_ref": f"outcome:{number}"})
    witnesses.append({**first, "image_digest": "distinct-image", "event_schema_version": 1})
    verification = {**verification, "witnesses": witnesses, "incident_ref": {"key": "fixed", "sha256": "fixed"}}
    before = deepcopy((scope.model_dump(mode="json"), verification))
    projected = recovery_context(scope, verification)
    assert (scope.model_dump(mode="json"), verification) == before
    assert projected["verification"]["rollback_context"] == verification["rollback_context"]
    assert projected["verification"]["incident_ref"] == verification["incident_ref"]
    summary = projected["verification"]["witness_summary"]
    assert summary["count"] == 630 and summary["sha256"] == _digest(witnesses)
    assert len(summary["groups"]) == 2
    assert summary["groups"][0]["count"] == 629
    assert summary["groups"][0]["first_observed_pair"]["input_ref"] == "input:0"
    assert summary["groups"][0]["last_observed_pair"]["outcome_ref"] == "outcome:628"
    assert summary["groups"][1]["image_digest"] == "distinct-image"
    for phase in ("baseline", "current"):
        original = before[0]["incident_observations"][phase]
        section = projected["scoping"]["incident_observations"][phase]
        assert "observations" not in section
        assert section["retained_observation_summary"]["sha256"] == _digest(original["observations"])
        expected_contracts = {
            _digest({"sha256": row["message"]["input_contract_sha256"], "descriptor": row["message"]["input_contract"]})
            for row in original["observations"]
            if "input_contract" in row["message"]
        }
        assert {
            _digest(contract) for contract in section["retained_observation_summary"]["distinct_input_contracts"]
        } == expected_contracts
        assert section["retained_observation_summary"]["distinct_source_manifests"]
        for key, value in original.items():
            if key != "observations":
                assert section[key] == value
    assert (
        projected["scoping"]["incident_observations"]["critical_facts"]
        == before[0]["incident_observations"]["critical_facts"]
    )


def test_real_workflow_projection_points_to_unchanged_immutable_raw_incident(part_store, monkeypatch, reference_scope):
    """The model gets summaries; actual S3-backed local store and returned part retain the complete proof."""
    orchestrator, container, _, run = wired_pipeline(part_store, monkeypatch)
    scope, _, _ = reference_scope
    orchestrator._run_scoping.return_value = scope
    container.incident_observer = Mock(
        refresh_current=Mock(return_value=scope.incident_observations.model_copy(deep=True))
    )
    captured = []

    def model(agent, prompt, output_model, timeout):
        """A local model double inspects exact input but cannot approve or replace server proof."""
        payload = json.loads(prompt)
        captured.append(payload)
        frozen = part_store.read_incident(run.rca_id)
        ref = payload["verification"]["incident_ref"]
        assert ref["key"] == frozen["record"]["payload_s3_key"]
        assert ref["sha256"] == frozen["record"]["payload_sha256"]
        original = frozen["payload"]["scoping"]["incident_observations"]
        assert original["current"]["observations"]
        assert "observations" not in payload["scoping"]["incident_observations"]["current"]
        return output_model.model_validate(
            {"title": "local", "summary": "declined", "reason": "fixture", "recommendation": "UNAVAILABLE"}
        )

    monkeypatch.setattr(analysis_roles, "invoke_agent", model)
    monkeypatch.setattr(analysis_workflow, "generate_code_preview", Mock(return_value={"status": "UNAVAILABLE"}))
    monkeypatch.setattr(analysis_workflow, "generate_operations", Mock(return_value={"title": "ops"}))
    assert orchestrator._run_pipeline_in_context(scope.raw_alarm, run)
    assert len(captured) == 1
    recovery = part_store.read_part(run.rca_id, "recovery")["payload"]["result"]
    verification = recovery["verification"]
    assert verification["witnesses"]
    assert _digest(verification) == captured[0]["verification"]["full_verification_sha256"]
    assert verification["rollback_context"] == captured[0]["verification"]["rollback_context"]
    assert recovery["playbook"] is None
