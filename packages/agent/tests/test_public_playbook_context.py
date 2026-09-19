"""Public model boundaries retain evidence authority without repeated raw receipt payloads."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from rca_agent.services import playbook_gen
from rca_agent.services.playbook_gen import _build_update_prompt, _build_user_prompt
from rca_agent.services.public_playbook_context import comparison_report, public_observations, public_part_proposals
from rca_agent.services.recovery_context import _digest
from tests.test_playbook_gen import _make_appraisal, _make_existing, _make_report
from tests.test_playbook_library import source

pytest_plugins = ["tests.test_recovery_reference", "tests.test_playbook_library"]


def test_repeated_raw_events_are_bound_without_changing_full_inputs(reference_scope):
    """Both draft and comparison retain exact evidence, critical facts and distinct descriptors."""
    scope, _, _ = reference_scope
    current = scope.incident_observations.current
    template = next(row for row in current["observations"] if "input_contract" in row["message"])
    original_rows = deepcopy(current["observations"])
    current["observations"].extend(
        {**deepcopy(template), "event_id": f"repeat-{number}", "request_id": f"request-{number}"}
        for number in range(629)
    )
    report = _make_report()
    reference = {"key": "analysis-parts/strands/rca-1/incident/fixture.json", "sha256": "a" * 64}
    report.analysis_parts = {"root_cause": {"payload": {"incident_ref": reference}}}
    before = deepcopy((scope.model_dump(mode="json"), report.model_dump(mode="json")))
    projected = public_observations(scope, reference)
    projected_current = projected["incident_observations"]["current"]
    summary = projected_current["retained_observation_summary"]
    assert summary["count"] == len(original_rows) + 629
    assert summary["sha256"] == _digest(current["observations"])
    assert summary["raw_source"]["incident_ref"] == reference
    assert summary["distinct_input_contracts"]
    for key, value in current.items():
        if key != "observations":
            assert projected_current[key] == value
    assert projected["incident_observations"]["critical_facts"] == before[0]["incident_observations"]["critical_facts"]
    for prompt in (
        _build_user_prompt(report, scope),
        _build_update_prompt(_make_existing(), report, scope),
    ):
        assert all(item in prompt for item in report.evidence_list)
        assert reference["key"] in prompt
        assert len(prompt.encode()) < 150_000
        assert "repeat-628" not in prompt
    assert (scope.model_dump(mode="json"), report.model_dump(mode="json")) == before


def test_proposals_keep_complete_content_and_bound_source_metadata():
    """Source bytes stay in the original part; every proposal, limitation and source hash remains."""
    report = _make_report()
    operations = {
        "summary": "Only this file inspected",
        "recommendations": [{"title": "new test", "status": "NOT_RUN", "limitation": "descendants unread"}],
        "observations": [{"status": "OBSERVED", "evidence_refs": ["ci:1"]}],
        "control_artifacts": [{"source_ref": "ci:1", "sha256": "b" * 64, "path": "ci.yml", "text": "ci " * 100_000}],
        "repository_collection": {"receipts": [{"body": "raw " * 100_000}], "warnings": ["partial collection"]},
    }
    code = {"status": "PROPOSED", "diff": "exact source diff", "test_status": "NOT_RUN"}
    report.analysis_parts = {
        "operations": {"payload": {"result": operations}},
        "root_cause": {"payload": {"result": {"code_proposal": code}}},
    }
    report.analysis_part_refs = {"operations": {"payload_s3_key": "immutable-part", "payload_sha256": "c" * 64}}
    before = deepcopy(report.model_dump(mode="json"))
    projected = public_part_proposals(report)
    assert projected["code_proposal"] == code
    assert projected["source_part_refs"] == report.analysis_part_refs
    result = projected["operations"]
    for key in ("summary", "recommendations", "observations"):
        assert result[key] == operations[key]
    assert result["control_sources"] == [{"source_ref": "ci:1", "sha256": "b" * 64, "path": "ci.yml"}]
    assert result["collection_warnings"] == ["partial collection"]
    assert result["original_evidence"]["result_sha256"] == _digest(operations)
    assert result["original_evidence"]["part_ref"] == report.analysis_part_refs["operations"]
    assert len(json.dumps(projected)) < 5_000
    assert report.model_dump(mode="json") == before


def test_large_role_sources_pass_real_comparison_archive_and_keep_reference_targets(
    storage, reference_scope, monkeypatch
):
    """The actual archive adapter accepts compared inputs; unused megabytes stay at immutable role references."""
    ddb, _, store = storage
    scope, _, _ = reference_scope
    report = _make_report()
    template = next(
        row for row in scope.incident_observations.current["observations"] if "input_contract" in row["message"]
    )
    scope.incident_observations.current["observations"].extend(
        {**deepcopy(template), "event_id": f"archive-repeat-{index}"} for index in range(629)
    )
    report.incident_observations = scope.incident_observations.model_copy(deep=True)
    report.analysis_parts = {
        "root_cause": {
            "payload": {
                "incident_ref": {"key": "frozen", "sha256": "a" * 64},
                "result": {"report_markdown": "raw" * 1_000_000},
            }
        },
        "operations": {
            "payload": {
                "result": {
                    "repository_collection": {"receipts": [{"body": "raw" * 1_000_000}]},
                    "recommendations": [{"status": "NOT_RUN"}],
                }
            }
        },
    }
    report.analysis_part_refs = {
        name: {"payload_s3_key": f"original/{name}.json", "payload_sha256": _digest(part["payload"])}
        for name, part in report.analysis_parts.items()
    }
    before = deepcopy(report.model_dump(mode="json"))
    assert len(json.dumps(before).encode()) > 400_000

    def model(agent, prompt, output_model, timeout, **kwargs):
        """Keep the model boundary small without replacing generation/comparison/archive logic."""
        assert len(prompt.encode()) < 150_000
        return output_model.model_validate(
            {"failure_type": "fixture", "symptom_pattern": "observed", "execution_steps": []}
        )

    monkeypatch.setattr(playbook_gen, "invoke_agent", model)
    monkeypatch.setattr(store, "search_similar", Mock(return_value=[]))
    draft = playbook_gen.run_playbook_generation(report, object(), playbook_store=store, scoping_result=scope)
    assert len(json.dumps(draft.comparison).encode()) < 300_000
    source(ddb, draft.model_dump(mode="json"))
    full, thin = playbook_gen.archive_incident_comparison(draft, store=store, rca_id=report.rca_id)
    assert "original_sk" in thin.comparison
    original = store._library._get("RCA#rca-1", thin.comparison["original_sk"])
    archived = json.loads(original["comparison_json"])
    assert archived == full.comparison
    assert archived["inputs"]["current_report"]["evidence_list"] == report.evidence_list
    assert archived["inputs"]["current_report"]["original_source"]["report_sha256"] == _digest(before)
    assert archived["inputs"]["current_report"]["original_source"]["source_part_refs"] == report.analysis_part_refs
    assert store.archive_comparison(thin, report.rca_id, "strands") == thin
    assert report.model_dump(mode="json") == before


def test_missing_immutable_part_references_does_not_discard_originals():
    """Legacy/incomplete provenance stays fully archived instead of claiming unavailable source locations."""
    report = _make_report()
    report.analysis_parts = {"root_cause": {"payload": {"result": {"raw": "original"}}}}
    assert comparison_report(report) == report.model_dump(mode="json")


def test_literal_choices_match_existing_guard_without_accepting_partial_quotes():
    """Display existing accepted strings verbatim, including JSON-style bracket refs; do not add IDs."""
    report = _make_report()
    report.evidence_list = ['Full observation\n["s3://bucket/original"]\nwith exact bytes', "[signal-2] Other fact"]
    choices = playbook_gen._literal_evidence_choices(report)
    values = json.loads(choices[choices.index('{"existing_bracket_references"') :])
    assert values["existing_bracket_references"] == ['["s3://bucket/original"]', "[signal-2]"]
    assert values["full_entries"] == report.evidence_list
    for value in values["existing_bracket_references"] + values["full_entries"]:
        assert playbook_gen._anchored_evidence(_make_appraisal(evidence=[value]), report) == [value]
    for invalid in ("Full observation", "s3://bucket/original", "Other fact", "[invented-id]"):
        with pytest.raises(ValueError, match="actually present"):
            playbook_gen._anchored_evidence(_make_appraisal(evidence=[invalid]), report)
    for prompt in (
        playbook_gen._build_user_prompt(report),
        playbook_gen._build_update_prompt(_make_existing(), report),
    ):
        assert choices in prompt


@pytest.mark.parametrize("confirmed", [True, False])
def test_comparison_labels_final_selection_without_rewriting_earlier_commentary(confirmed):
    """Final state is copied exactly; earlier uncertainty stays available as source text, not decision authority."""
    from rca_agent.services.public_playbook_context import comparison_decision_context

    report = _make_report()
    report.root_cause_confirmed = confirmed
    report.selected_hypothesis_id = "selected"
    report.selected_hypothesis_title = "Measured mechanism"
    report.evidence_list = ["[earlier] This candidate still needs investigation", "[selected] Observed mismatch"]
    before = deepcopy(report.model_dump(mode="json"))
    context = comparison_decision_context(report)
    assert context["final_selected_root"]["confirmed"] is confirmed
    assert context["final_selected_root"]["hypothesis_id"] == "selected"
    assert context["final_selected_root"]["root_cause"] == report.root_cause
    assert context["retained_commentary"]["source_path"] == "current_report#/evidence_list"
    prompt = playbook_gen._build_update_prompt(_make_existing(), report)
    assert prompt.startswith("## Final selection and source-stage provenance")
    assert all(entry in prompt for entry in report.evidence_list)
    assert report.model_dump(mode="json") == before
    assert "Final selection and source-stage provenance" not in playbook_gen._build_user_prompt(report)
