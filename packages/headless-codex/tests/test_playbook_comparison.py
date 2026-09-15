"""Exercise actual comparison inputs and immutable proposals through the incident pipeline."""

import json
import time
import uuid
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_artifact_validation import _playbook
from test_pipeline import _write_confirmed_report_artifacts

from headless_codex.services import playbook_comparison as comparison
from headless_codex.services.analysis_contract import validate_analysis_completion
from headless_codex.services.artifact_validation import render_completion_report, validate_completion_artifacts


def model_judgment(payload, *, selected=None, update=None):
    identifier = selected if selected is not None else payload["candidates"][0]["playbook_id"]
    return {
        "candidates": [
            {
                "playbook_id": candidate["playbook_id"],
                "applicable": candidate["playbook_id"] == identifier,
                "rationale": "Observed lock owner matches the precondition."
                if candidate["playbook_id"] == identifier
                else "Current lock evidence contradicts the CPU mechanism.",
            }
            for candidate in payload["candidates"]
        ],
        "selected_playbook_id": identifier,
        "knowledge_update": update or {},
        "rationale": "Current owner evidence supplies the missing escalation check.",
        "evidence": [payload["evidence"][0]["ref"]],
    }


@pytest.fixture
def incident(tmp_path):
    _write_confirmed_report_artifacts(tmp_path)
    artifacts = validate_completion_artifacts(tmp_path)
    existing = _playbook(
        playbook_id="published",
        failure_type="DB connection leak",
        temporary_mitigation="Preserved retrospective learning",
        rca_id="historical-incident",
        verification_status="VERIFIED",
        library_revision="revision-7",
        source_engine="strands",
        source_rca_id="historical-incident",
    )
    hit = SimpleNamespace(playbook_id="published", rca_id="historical-incident", engine="strands", similarity=0.93)
    store = SimpleNamespace(search_similar=Mock(return_value=[hit]), load_detail=Mock(return_value=existing))
    runner = SimpleNamespace(compare_playbooks=Mock(side_effect=lambda payload, **_: model_judgment(payload)))
    return SimpleNamespace(
        base=tmp_path,
        current=artifacts.playbook,
        existing=existing,
        store=store,
        runner=runner,
        hit=hit,
    )


def run_comparison(incident, **overrides):
    arguments = {
        "store": incident.store,
        "runner": incident.runner,
        "metric_name": "DatabaseConnections",
        "artifact_dir": incident.base,
        "analysis": validate_analysis_completion(incident.base).effective_state_view(),
        "execution_token": "a" * 32,
        "deadline": time.monotonic() + 100,
        "cancel_checker": lambda: False,
        **overrides,
    }
    return comparison.compare_incident_playbook(incident.current, **arguments)


def test_proposal_uses_exact_historical_baseline_and_actual_current_evidence(incident):
    incident.existing["comparison"] = {"old": "metadata"}
    incident.runner.compare_playbooks.side_effect = lambda payload, **_: model_judgment(
        payload, update={"escalation_criteria": "Check the observed transaction owner before escalating"}
    )
    original = deepcopy(incident.current)
    old = deepcopy(incident.existing)
    files = {p.name: p.read_bytes() for p in incident.base.iterdir()}

    result = run_comparison(incident)

    wire = result["comparison"]
    assert wire["status"] == "UPDATE_PROPOSED"
    assert wire["selected_playbook_id"] == result["playbook_id"] == "published"
    assert result["library_revision"] == "revision-7"
    assert result["temporary_mitigation"] == old["temporary_mitigation"]
    assert result["escalation_criteria"] == old["escalation_criteria"]
    assert result["execution_steps"] == original["execution_steps"]
    proposal = wire["proposal"]
    assert str(uuid.UUID(proposal["proposal_id"])) == proposal["proposal_id"]
    assert proposal["state"] == "PENDING"
    assert proposal["base_revision"] == "revision-7"
    assert proposal["before"] == comparison.historical_snapshot(old)
    assert proposal["after"]["execution_steps"] == old["execution_steps"]
    assert proposal["after"]["verification_status"] == "VERIFIED"
    assert proposal["after"]["rca_id"] == "historical-incident"
    assert not (set(proposal["after"]) & comparison.AUXILIARY_FIELDS)
    assert proposal["changes"] == [
        {
            "field": "escalation_criteria",
            "before": old["escalation_criteria"],
            "after": "Check the observed transaction owner before escalating",
        }
    ]
    payload = incident.runner.compare_playbooks.call_args.args[0]
    assert payload["candidates"][0]["playbook"] == proposal["before"]
    assert payload["analysis"]["root_cause_confirmed"] is True
    for ref in proposal["evidence"]:
        source, pointer = ref.split("#", 1)
        value = json.loads(files[source])
        for part in pointer.lstrip("/").split("/"):
            value = value[int(part)] if isinstance(value, list) else value[part]
        assert next(item["value"] for item in payload["evidence"] if item["ref"] == ref) == value
    assert incident.current == original
    assert incident.existing == old
    assert {p.name: p.read_bytes() for p in incident.base.iterdir()} == files


def test_no_change_reuses_existing_identity_and_public_knowledge(incident):
    result = run_comparison(incident)
    assert result["playbook_id"] == "published"
    assert result["comparison"]["status"] == "NO_CHANGE"
    assert "proposal" not in result["comparison"]
    assert result["temporary_mitigation"] == "Preserved retrospective learning"
    assert result["verification_status"] == "DRAFT"


@pytest.mark.parametrize("field", sorted(comparison.KNOWLEDGE_LISTS))
def test_proposed_list_preserves_prior_entries_and_diff_matches_after(incident, field):
    """Omission and duplicate model entries cannot delete or duplicate accumulated knowledge."""
    incident.existing[field] = ["old-first", "old-second"]
    incident.runner.compare_playbooks.side_effect = lambda payload, **_: model_judgment(
        payload, update={field: ["new", "old-second", "new"]}
    )
    result = run_comparison(incident)
    proposal = result["comparison"]["proposal"]
    assert proposal["after"][field] == ["old-first", "old-second", "new"]
    assert proposal["changes"] == [
        {"field": field, "before": ["old-first", "old-second"], "after": ["old-first", "old-second", "new"]}
    ]
    assert result[field] == ["old-first", "old-second"]
    assert proposal["after"]["execution_steps"] == incident.existing["execution_steps"]


@pytest.mark.parametrize("update", [{}, {"tags": ["new"]}])
def test_used_references_freeze_baseline_and_cited_values_after_input_changes(incident, update):
    """Both outcomes retain actual inputs; searched-only candidates never become generation references."""
    incident.runner.compare_playbooks.side_effect = lambda payload, **_: model_judgment(payload, update=update)
    rejected = SimpleNamespace(playbook_id="searched-only", rca_id="other", engine="strands", similarity=0.99)
    incident.store.search_similar.return_value = [rejected, incident.hit]
    incident.store.load_detail.side_effect = [None, incident.existing]
    result = run_comparison(incident, rca_id="current-rca")
    wire = result["comparison"]
    payload = incident.runner.compare_playbooks.call_args.args[0]
    assert wire["inputs"] == payload
    runbook_reference = next(item for item in wire["used_references"] if item["role"] == "current-runbook-input")
    assert runbook_reference == {
        "role": "current-runbook-input",
        "ref": "analysis",
        "source_rca_id": "current-rca",
        "source_engine": "headless-codex",
    }
    assert wire["inputs"][runbook_reference["ref"]] == payload["analysis"]
    assert "current_report" not in wire["inputs"]
    assert wire["baseline"] == {
        "playbook_id": "published",
        "revision": "revision-7",
        "source_rca_id": "historical-incident",
        "source_engine": "strands",
        "playbook": comparison.historical_snapshot(incident.existing),
    }
    assert wire["evidence"] == [
        {**payload["evidence"][0], "source_rca_id": "current-rca", "source_engine": "headless-codex"}
    ]
    assert {item["role"] for item in wire["used_references"]} == {
        "knowledge-reuse",
        "current-runbook-input",
        "comparison-evidence",
    }
    assert [item["playbook_id"] for item in wire["used_references"] if item["role"] == "knowledge-reuse"] == [
        "published"
    ]
    frozen = deepcopy(wire)
    incident.existing["execution_steps"].clear()
    payload["evidence"][0]["value"] = "later value"
    for path in incident.base.glob("*.json"):
        path.unlink()
    assert wire == frozen


@pytest.mark.parametrize("has_match", [False, True])
def test_current_runbook_reference_uses_validated_analysis_without_reading_report_cooutput(incident, has_match):
    """The generated report is not a runbook input; references resolve to the supplied, frozen verdict."""
    analysis = validate_analysis_completion(incident.base).effective_state_view()
    (incident.base / "report.md").unlink()
    if not has_match:
        incident.store.search_similar.return_value = []
    result = run_comparison(incident, analysis=analysis, rca_id="trusted-current-rca")
    wire = result["comparison"]
    assert wire["status"] == ("NO_CHANGE" if has_match else "NO_MATCH")
    reference = next(item for item in wire["used_references"] if item["role"] == "current-runbook-input")
    assert reference["ref"] == "analysis"
    assert reference["source_rca_id"] == "trusted-current-rca"
    assert wire["inputs"][reference["ref"]] == analysis
    frozen = deepcopy(wire["inputs"]["analysis"])
    analysis.clear()
    assert wire["inputs"]["analysis"] == frozen
    assert not any(item["ref"] == "current_report" for item in wire["used_references"])


@pytest.mark.parametrize("steps", [[], [{"step_id": "new", "commands": ["current"], "extra_binding": "current-only"}]])
def test_current_runbook_is_preserved_whole_even_when_empty(incident, steps):
    incident.current["execution_steps"] = steps
    result = run_comparison(incident)
    assert result["execution_steps"] == steps
    assert result["verification_status"] == "DRAFT"


def test_identical_current_runbook_retains_only_recorded_verification(incident):
    incident.existing["execution_steps"] = deepcopy(incident.current["execution_steps"])
    result = run_comparison(incident)
    assert result["verification_status"] == "VERIFIED"
    assert result["execution_steps"] == incident.current["execution_steps"]


def test_model_can_reject_more_similar_candidate_and_select_another(incident):
    other = deepcopy(incident.hit)
    other.playbook_id = "different-mechanism"
    other.similarity = 0.99
    incident.store.search_similar.return_value = [other, incident.hit]
    wrong = {**incident.existing, "playbook_id": other.playbook_id, "failure_type": "CPU saturation"}
    incident.store.load_detail.side_effect = [wrong, incident.existing]
    incident.runner.compare_playbooks.side_effect = lambda payload, **_: model_judgment(payload, selected="published")
    result = run_comparison(incident)
    assert result["playbook_id"] == "published"
    assert [item["applicable"] for item in result["comparison"]["candidates"]] == [False, True]


def test_inapplicable_candidates_create_new_incident_playbook_with_explanations(incident):
    incident.runner.compare_playbooks.side_effect = lambda payload, **_: model_judgment(payload, selected="")
    result = run_comparison(incident)
    assert result["comparison"]["status"] == "NO_APPLICABLE_MATCH"
    assert result["comparison"]["selected_playbook_id"] == ""
    assert result["playbook_id"] == incident.current["playbook_id"]
    assert result["comparison"]["candidates"][0]["rationale"]


def test_unavailable_search_hit_preserves_exact_reason_without_becoming_used_reference(incident):
    """The adapter's inaccessible hit stays in the audit even though its original cannot be compared."""
    incident.hit.unavailable_reason = "published detail lookup failed (AccessDeniedException)"
    result = run_comparison(incident)
    assert result["comparison"]["status"] == "SEARCH_FAILED"
    assert result["comparison"]["candidates"][0]["rationale"] == incident.hit.unavailable_reason
    assert all(item["role"] != "knowledge-reuse" for item in result["comparison"]["used_references"])
    incident.store.load_detail.assert_not_called()
    incident.runner.compare_playbooks.assert_not_called()


@pytest.mark.parametrize(
    "failure,status",
    [
        ("no_matches", "NO_MATCH"),
        ("search", "SEARCH_FAILED"),
        ("detail_none", "SEARCH_FAILED"),
        ("detail_error", "SEARCH_FAILED"),
        ("revision_missing", "SEARCH_FAILED"),
        ("model_error", "SEARCH_FAILED"),
        ("malformed", "SEARCH_FAILED"),
        ("timeout", "SEARCH_FAILED"),
        ("cancel", "SEARCH_FAILED"),
    ],
)
def test_failures_are_explicit_and_preserve_the_completed_report_and_runbook(incident, failure, status):
    options = {}
    if failure == "no_matches":
        incident.store.search_similar.return_value = []
    elif failure == "search":
        incident.store.search_similar.side_effect = RuntimeError("index unavailable")
    elif failure == "detail_none":
        incident.store.load_detail.return_value = None
    elif failure == "detail_error":
        incident.store.load_detail.side_effect = RuntimeError("detail unavailable")
    elif failure == "revision_missing":
        incident.existing.pop("library_revision")
    elif failure == "model_error":
        incident.runner.compare_playbooks.side_effect = TimeoutError("model deadline")
    elif failure == "malformed":
        incident.runner.compare_playbooks.side_effect = lambda *_, **__: "not JSON"
    elif failure == "timeout":
        options["deadline"] = time.monotonic() - 1
    elif failure == "cancel":
        options["cancel_checker"] = lambda: True
    result = run_comparison(incident, **options)
    assert result["comparison"]["status"] == status
    assert result["comparison"]["selected_playbook_id"] == ""
    assert result["execution_steps"] == incident.current["execution_steps"]
    assert result["playbook_id"] == incident.current["playbook_id"]
    assert "## 5 Whys" in render_completion_report(incident.base, result)
    if failure in {"timeout", "cancel", "search", "detail_none", "detail_error", "revision_missing", "no_matches"}:
        incident.runner.compare_playbooks.assert_not_called()


def test_unavailable_candidate_does_not_prevent_other_actual_details_being_compared(incident):
    absent = deepcopy(incident.hit)
    absent.playbook_id = "expired"
    incident.store.search_similar.return_value = [absent, incident.hit]
    incident.store.load_detail.side_effect = [None, incident.existing]
    result = run_comparison(incident)
    assert result["comparison"]["status"] == "NO_CHANGE"
    assert result["comparison"]["candidates"][0]["availability"] == "UNAVAILABLE"
    assert result["comparison"]["candidates"][0]["applicable"] is None
    payload = incident.runner.compare_playbooks.call_args.args[0]
    assert [item["playbook_id"] for item in payload["candidates"]] == ["published"]


@pytest.mark.parametrize(
    "corruption",
    [
        "unknown_id",
        "missing_candidate",
        "no_rationale",
        "no_evidence",
        "fake_ref",
        "execution_update",
        "erase",
        "status",
    ],
)
def test_model_cannot_fabricate_baselines_or_change_execution_or_publish(incident, corruption):
    def judge(payload, **_):
        value = model_judgment(payload)
        if corruption == "unknown_id":
            value["selected_playbook_id"] = "invented"
        elif corruption == "missing_candidate":
            value["candidates"] = []
        elif corruption == "no_rationale":
            value["candidates"][0]["rationale"] = ""
        elif corruption == "no_evidence":
            value["evidence"] = []
        elif corruption == "fake_ref":
            value["evidence"] = ["validation-9.json#/confirmed/0/evidence_summary/0"]
        elif corruption == "execution_update":
            value["knowledge_update"] = {"execution_steps": []}
        elif corruption == "erase":
            value["knowledge_update"] = {"temporary_mitigation": ""}
        elif corruption == "status":
            value["state"] = "APPLIED"
        return value

    incident.runner.compare_playbooks.side_effect = judge
    result = run_comparison(incident)
    assert result["comparison"]["status"] == "SEARCH_FAILED"
    assert "proposal" not in result["comparison"]
    assert result["comparison"]["selected_playbook_id"] == ""


def test_input_budget_does_not_silently_truncate_historical_details(incident, monkeypatch):
    run_comparison(incident)
    payload = incident.runner.compare_playbooks.call_args.args[0]
    monkeypatch.setattr(comparison, "MAX_COMPARISON_INPUT_BYTES", 10)
    with pytest.raises(ValueError, match="not truncated"):
        comparison.comparison_prompt(payload)


def test_duplicate_json_keys_are_not_accepted(incident):
    run_comparison(incident)
    payload = incident.runner.compare_playbooks.call_args.args[0]
    with pytest.raises(ValueError, match="duplicate"):
        comparison.validate_model_comparison('{"candidates": [], "candidates": []}', payload)


def test_no_usable_current_evidence_does_not_invent_a_comparison(incident, monkeypatch):
    monkeypatch.setattr(comparison, "current_evidence", lambda _: [])
    result = run_comparison(incident)
    assert result["comparison"]["status"] == "SEARCH_FAILED"
    incident.runner.compare_playbooks.assert_not_called()


def test_proposal_snapshot_size_failure_keeps_new_report_deliverable(incident, monkeypatch):
    monkeypatch.setattr(comparison, "MAX_INCIDENT_PLAYBOOK_BYTES", 10)
    result = run_comparison(incident)
    assert result["comparison"]["status"] == "SEARCH_FAILED"
    assert result["comparison"]["selected_playbook_id"] == ""
    assert result["execution_steps"] == incident.current["execution_steps"]
    assert "## 빠른 판단" in render_completion_report(incident.base, result)


def test_model_output_budget_failure_is_explicit(incident):
    def oversized(payload, **_):
        value = model_judgment(payload)
        value["rationale"] = "x" * (comparison.MAX_COMPARISON_OUTPUT_BYTES + 1)
        return value

    incident.runner.compare_playbooks.side_effect = oversized
    assert run_comparison(incident)["comparison"]["status"] == "SEARCH_FAILED"


def test_all_unavailable_or_rejected_does_not_claim_every_candidate_was_inapplicable(incident):
    absent = deepcopy(incident.hit)
    absent.playbook_id = "expired"
    incident.store.search_similar.return_value = [absent, incident.hit]
    incident.store.load_detail.side_effect = [None, incident.existing]
    incident.runner.compare_playbooks.side_effect = lambda payload, **_: model_judgment(payload, selected="")
    result = run_comparison(incident)
    assert result["comparison"]["status"] == "SEARCH_FAILED"
    assert [item["applicable"] for item in result["comparison"]["candidates"]] == [None, False]


def test_new_model_metadata_cannot_supply_a_library_baseline(incident):
    incident.current.update(library_revision="invented", source_rca_id="invented", source_engine="invented")
    incident.store.search_similar.return_value = []
    result = run_comparison(incident)
    assert "library_revision" not in result
    assert "source_rca_id" not in result
    assert "source_engine" not in result
