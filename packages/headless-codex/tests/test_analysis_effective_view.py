"""Regression tests for authoritative selection across real server validation loops."""

from headless_codex import eval_adapter
from headless_codex.services.analysis_contract import validate_analysis_completion

SCENARIO = {"observations": [{"id": "selected"}, {"id": "other"}, {"id": "counter"}]}


def test_root_normalizer_uses_server_selection_not_first_confirmed(analysis_artifacts, save_validation, judgment):
    """A weaker confirmed entry must not replace the server-selected stronger root."""
    save_validation(
        1,
        confirmed=[
            judgment("other", 0.81, fault_type="slow-query", reasoning="[other]"),
            judgment("root", 0.97, reasoning="[selected]"),
        ],
        rejected=[judgment("third", 0.1, reasoning="[counter]")],
    )
    result = validate_analysis_completion(analysis_artifacts)
    assert result.selected_hypothesis.hypothesis_id == "root"
    assert eval_adapter._root_fault_type(analysis_artifacts) == "unsupported"
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == ["selected"]


def test_root_normalizer_retains_selected_judgment_across_grace_loops(analysis_artifacts, save_validation, judgment):
    """A confirmed root need not be repeated in the last delta validation artifact."""
    save_validation(1, confirmed=[judgment("root", 0.85, fault_type="db-leak", reasoning="[selected]")])
    save_validation(2, rejected=[judgment("other", 0.1, reasoning="[other]")])
    final = save_validation(3, rejected=[judgment("third", 0.1, reasoning="[counter]")])
    assert final["server_decision"]["reason"] == "REVIEW_GATE_GRACE_EXHAUSTED"
    assert not final["confirmed"]
    assert eval_adapter._root_fault_type(analysis_artifacts) == "db-leak"
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == ["selected"]


def test_effective_view_preserves_actual_closed_evidence_and_failure(analysis_artifacts, save_validation, judgment):
    """A failed-collection candidate stays CLOSED with its actual evidence, never a new rejection."""
    save_validation(
        1,
        confirmed=[
            judgment("root", 0.97, reasoning="[selected] root reasoning", evidence=["actual root evidence"]),
            judgment("other", 0.99, reasoning="query failed", evidence=["partial observation"], failed=True),
        ],
    )
    result = validate_analysis_completion(analysis_artifacts)
    view = result.effective_state_view()
    root, other, third = view["hypotheses"]
    assert root["reasoning"] == "[selected] root reasoning"
    assert root["evidence_summary"] == ["actual root evidence"]
    assert other["status"] == "closed"
    assert other["reasoning"] == "Server termination: CONFIRMED"
    assert other["evidence_collection_failed"] is True
    assert other["evidence_summary"] == ["partial observation"]
    assert other["fault_type"] is None
    assert third["evidence_summary"] == []
    assert all(h["status"] != "rejected" for h in view["hypotheses"])
    root["evidence_summary"].append("not an observation")
    assert result.snapshot.evidence_summaries["root"] == ["actual root evidence"]


def test_unconfirmed_terminal_candidate_does_not_create_root_citations(analysis_artifacts, save_validation, judgment):
    """A selected candidate at the cap is not a confirmed root even when it has citations."""
    for loop in range(1, 4):
        save_validation(loop, needs_investigation=[judgment("root", 0.7, reasoning="[selected] not conclusive")])
    assert not validate_analysis_completion(analysis_artifacts).confirmed
    assert eval_adapter._root_fault_type(analysis_artifacts) == "unsupported"
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == []
