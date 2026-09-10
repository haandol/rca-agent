"""Regression tests for authoritative selection across real server validation loops."""

import json

import pytest

from headless_codex import eval_adapter
from headless_codex.adapters.secondary.codex import codex_subprocess_runner
from headless_codex.ports.dto.models import CodexResult
from headless_codex.services.analysis_contract import validate_analysis_completion

SCENARIO = {"observations": [{"id": "selected"}, {"id": "other"}, {"id": "counter"}]}


def _report_state(monkeypatch, analysis_artifacts):
    """Capture the real Report handoff while replacing only external specialist execution."""
    prompts = []

    def run_specialist(prompt, **kwargs):
        prompts.append(prompt)
        return CodexResult(success=True, result="specialist summary", raw_output="")

    runner = codex_subprocess_runner.CodexSubprocessRunner()
    monkeypatch.setattr(runner, "_run_single", run_specialist)
    monkeypatch.setattr(codex_subprocess_runner, "artifact_dir_for_token", lambda _token: analysis_artifacts)
    assert runner.run("investigate", execution_token="a" * 32).success
    assert len(prompts) == 2
    return json.JSONDecoder().raw_decode(prompts[1].split("[서버 검증 유효 상태 — 판정의 권위]\n")[1])[0]


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


@pytest.mark.parametrize("closure_reason", ["CONFIRMED", "MAX_LOOPS"])
@pytest.mark.parametrize("prior_judgment", [False, True])
def test_effective_view_preserves_actual_closed_evidence_and_failure(
    monkeypatch, analysis_artifacts, save_validation, judgment, closure_reason, prior_judgment
):
    """Persist full direct judgments through closure, replay and Report, including earlier-loop evidence."""
    reasoning = (
        "CANARY_DIRECT_REASON: [obs-17] collection denied; alternate cause remains untested\n"
        + "관측 수집 실패의 실제 상세 근거. " * 400
        + "[obs-tail] final citation"
    )
    evidence = ["partial observation [obs-17]", "실제 관측 내용 " * 400 + "[obs-tail]"]
    other_judgment = judgment("other", 0.99, reasoning=reasoning, evidence=evidence, failed=True)
    final_loop = 3 if closure_reason == "MAX_LOOPS" else 2 if prior_judgment else 1
    for loop in range(1, final_loop):
        save_validation(loop, confirmed=[other_judgment] if prior_judgment and loop == 1 else [])
    saved = save_validation(
        final_loop,
        confirmed=(
            [judgment("root", 0.97, reasoning="[selected] root reasoning", evidence=["actual root evidence"])]
            if closure_reason == "CONFIRMED"
            else []
        )
        + ([] if prior_judgment else [other_judgment]),
    )
    persisted = json.loads((analysis_artifacts / f"validation-{final_loop}.json").read_text())
    assert persisted == saved
    closed = next(entry for entry in persisted["closed"] if entry["hypothesis_id"] == "other")
    assert closed["reasoning"] == reasoning
    assert closed["evidence_summary"] == evidence
    assert closed["closure_reason"] == closure_reason
    assert closed["confidence"] == 0.99
    assert closed["evidence_collection_failed"] is True
    assert closed["server_closed"] is True
    assert persisted["rejected"] == []
    assert persisted["server_decision"]["reason"] == closure_reason
    assert persisted["server_decision"]["action"] == "REPORT"
    result = validate_analysis_completion(analysis_artifacts)
    assert result.latest_validation == persisted
    assert result.snapshot.reasoning["other"] == reasoning
    assert result.snapshot.evidence_summaries["other"] == evidence
    assert result.confirmed is (closure_reason == "CONFIRMED")
    assert result.selected_hypothesis.hypothesis_id == ("root" if result.confirmed else "other")
    assert result.selected_confidence == (0.97 if result.confirmed else 0.99)
    view = result.effective_state_view()
    assert _report_state(monkeypatch, analysis_artifacts) == view
    root, other, third = view["hypotheses"]
    if result.confirmed:
        assert root["status"] == "confirmed"
        assert root["reasoning"] == "[selected] root reasoning"
        assert root["evidence_summary"] == ["actual root evidence"]
        assert root["closure_reason"] is None
    else:
        assert root["status"] == "closed"
        assert root["reasoning"] == ""
    assert other["status"] == "closed"
    assert other["reasoning"] == reasoning
    assert other["closure_reason"] == closure_reason
    assert other["evidence_collection_failed"] is True
    assert other["evidence_summary"] == evidence
    assert other["fault_type"] is None
    assert third["status"] == "closed"
    assert third["reasoning"] == ""
    assert third["closure_reason"] == closure_reason
    assert third["evidence_summary"] == []
    assert all(h["status"] != "rejected" for h in view["hypotheses"])
    other["evidence_summary"].append("not an observation")
    assert result.snapshot.evidence_summaries["other"] == evidence


@pytest.mark.parametrize("legacy_reason", [None, "", "Server termination: CONFIRMED"])
def test_legacy_closure_does_not_invent_missing_direct_reasoning(
    monkeypatch, analysis_artifacts, save_validation, judgment, legacy_reason
):
    """Legacy missing or overwritten reasoning stays absent even when earlier judgments exist."""
    save_validation(1, needs_investigation=[judgment("other", 0.6, reasoning="[earlier] incomplete")])
    saved = save_validation(2, confirmed=[judgment("root", 0.97, reasoning="[selected]")])
    for entry in saved["closed"]:
        entry.pop("closure_reason", None)
        if legacy_reason is None:
            entry.pop("reasoning", None)
        else:
            entry["reasoning"] = legacy_reason
    path = analysis_artifacts / "validation-2.json"
    path.write_text(json.dumps(saved))
    original = path.read_text()
    result = validate_analysis_completion(analysis_artifacts)
    view = result.effective_state_view()
    assert _report_state(monkeypatch, analysis_artifacts) == view
    for entry in view["hypotheses"][1:]:
        assert entry["status"] == "closed"
        assert entry["reasoning"] == ""
        assert entry["closure_reason"] == "CONFIRMED"
    assert view["hypotheses"][1]["evidence_summary"] == ["observed"]
    assert result.snapshot.reasoning["other"] == ""
    assert path.read_text() == original


def test_unconfirmed_terminal_candidate_does_not_create_root_citations(analysis_artifacts, save_validation, judgment):
    """A selected candidate at the cap is not a confirmed root even when it has citations."""
    for loop in range(1, 4):
        save_validation(loop, needs_investigation=[judgment("root", 0.7, reasoning="[selected] not conclusive")])
    assert not validate_analysis_completion(analysis_artifacts).confirmed
    assert eval_adapter._root_fault_type(analysis_artifacts) == "unsupported"
    assert eval_adapter._root_cause_evidence_ids(analysis_artifacts, SCENARIO) == []
