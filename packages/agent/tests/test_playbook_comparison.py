"""Behavior contracts for evidence-backed proposals that never publish knowledge."""

import json
from copy import deepcopy
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from rca_agent.ports.dto.models import ExecutionStep, Playbook, PlaybookVerificationStatus
from rca_agent.services.playbook_gen import (
    ExecutionStepOutput,
    PlaybookOutput,
    PlaybookUpdateOutput,
    run_playbook_generation,
)
from tests.test_playbook_gen import _make_appraisal, _make_existing, _make_hit, _make_report
from tests.test_runbook_contract import command_step, wait_step


def _draft():
    commands = command_step()
    commands["commands"] = [command.replace("incident-owner", "current-owner") for command in commands["commands"]]
    return PlaybookOutput(
        failure_type="Memory leak",
        symptom_pattern="CPU spike + memory growth",
        execution_steps=[ExecutionStepOutput(**step) for step in [commands, wait_step()]],
    )


def _run(hits, *, details=None, appraisals=(), search_error=None, report=None):
    draft = _draft()
    outputs = [draft, *appraisals]
    agent = MagicMock(side_effect=[MagicMock(structured_output=output) for output in outputs])
    store = MagicMock()
    store.search_similar.return_value = hits
    store.search_similar.side_effect = search_error
    store.load_detail.side_effect = details or [_make_existing(playbook_id=hit.playbook_id) for hit in hits]
    result = run_playbook_generation(report or _make_report(), agent, playbook_store=store)
    store.save.assert_not_called()
    return result, agent, store, draft


def test_high_similarity_inapplicable_candidate_is_actually_appraised_and_excluded():
    first = _make_hit(playbook_id="traffic", similarity=0.98)
    second = _make_hit(playbook_id="leak", similarity=0.84)
    rejected = _make_appraisal(applicable=False, rationale="메모리 증가가 있어 CPU 트래픽 증가만의 대응은 맞지 않는다.")
    selected = _make_appraisal(needs_update=True, related_metrics=["HeapUsage"])

    result, agent, store, draft = _run([first, second], appraisals=[rejected, selected])

    assert result.playbook_id == "leak"
    assert result.comparison["status"] == "UPDATE_PROPOSED"
    assert result.comparison["selected_playbook_id"] == "leak"
    candidates = result.comparison["candidates"]
    assert [(c["playbook_id"], c["applicable"]) for c in candidates] == [("traffic", False), ("leak", True)]
    assert rejected.rationale in candidates[0]["rationale"]
    assert all("memory growth" in c["rationale"] for c in candidates)
    assert store.load_detail.call_count == 2
    assert [call.kwargs["structured_output_model"] for call in agent.call_args_list] == [
        PlaybookOutput,
        PlaybookUpdateOutput,
        PlaybookUpdateOutput,
    ]
    assert "Critical if OOM kills detected" in agent.call_args_list[1].args[0]
    assert "high CPU" in agent.call_args_list[1].args[0]
    assert "applicable=false" in agent.call_args_list[1].args[0]
    assert result.execution_steps == [ExecutionStep(**step.model_dump()) for step in draft.execution_steps]


@pytest.mark.parametrize(
    ("hits", "appraisals", "error", "status"),
    [
        ([], [], None, "NO_MATCH"),
        ([], [], RuntimeError("search disabled"), "SEARCH_FAILED"),
        ([_make_hit()], [_make_appraisal(applicable=False)], None, "NO_APPLICABLE_MATCH"),
        ([_make_hit()], [_make_appraisal(evidence=["[invented-reference]"])], None, "SEARCH_FAILED"),
        ([_make_hit()], [None], None, "SEARCH_FAILED"),
    ],
)
def test_new_draft_preserves_current_plan_and_explicit_retrieval_outcome(hits, appraisals, error, status):
    result, _, _, draft = _run(hits, appraisals=appraisals, search_error=error)
    assert result.playbook_id != "existing-1"
    assert result.comparison["status"] == status
    assert result.comparison["query"]
    assert result.comparison["selected_playbook_id"] == ""
    assert "proposal" not in result.comparison
    assert result.execution_steps == [ExecutionStep(**step.model_dump()) for step in draft.execution_steps]
    if status == "SEARCH_FAILED" and hits:
        assert result.comparison["candidates"][0]["applicable"] is None


@pytest.mark.parametrize("unavailable", [None, RuntimeError("lookup failed")])
def test_unreadable_candidate_is_recorded_and_never_sent_to_model(unavailable):
    result, agent, _, _ = _run([_make_hit()], details=[unavailable])
    candidate = result.comparison["candidates"][0]
    assert candidate["availability"] == "UNAVAILABLE"
    assert candidate["applicable"] is None
    assert candidate["rationale"]
    assert result.comparison["status"] == "SEARCH_FAILED"
    assert agent.call_count == 1


def test_unavailable_search_hit_preserves_exact_reason_without_becoming_used_reference():
    """A positive similarity hit with unavailable detail remains a failure audit, not a no-match result."""
    hit = _make_hit()
    hit.unavailable_reason = "published detail lookup failed (AccessDeniedException)"
    result, agent, store, _ = _run([hit])
    assert result.comparison["status"] == "SEARCH_FAILED"
    assert result.comparison["candidates"][0]["rationale"] == hit.unavailable_reason
    assert all(item["role"] != "knowledge-reuse" for item in result.comparison["used_references"])
    store.load_detail.assert_not_called()
    assert agent.call_count == 1


def test_all_candidates_are_recorded_even_after_an_applicable_match():
    hits = [_make_hit(playbook_id=f"p-{i}", similarity=0.9 - i / 100) for i in range(3)]
    result, agent, _, _ = _run(
        hits, appraisals=[_make_appraisal(), _make_appraisal(applicable=False), _make_appraisal()]
    )
    assert agent.call_count == 4
    assert result.playbook_id == "p-0"
    assert result.comparison["status"] == "NO_CHANGE"
    assert len(result.comparison["candidates"]) == 3
    assert "다른 후보" in result.comparison["candidates"][2]["rationale"]
    assert "proposal" not in result.comparison


def test_proposal_freezes_historical_document_and_only_changes_knowledge():
    existing = _make_existing(
        library_revision="revision-7",
        source_engine="headless-codex",
        source_rca_id="original-rca",
        execution_steps=[ExecutionStep(**command_step()), ExecutionStep(**wait_step())],
        verification_status=PlaybookVerificationStatus.VERIFIED,
        comparison={"status": "NO_MATCH", "candidates": []},
    )
    previous = existing.model_dump(mode="json")
    appraisal = _make_appraisal(
        needs_update=True,
        verification_steps=["Inspect heap"],  # An omitted old entry must survive.
        permanent_remediation="Fix code and add a leak regression test",
        execution_steps=_draft().execution_steps,  # Must not change historical snapshots.
    )
    result, _, _, draft = _run([_make_hit()], details=[existing], appraisals=[appraisal])
    proposal = result.comparison["proposal"]
    assert str(UUID(proposal["proposal_id"])) == proposal["proposal_id"]
    assert proposal["source_engine"] == "headless-codex"
    assert proposal["source_rca_id"] == "original-rca"
    assert proposal["base_revision"] == result.library_revision == "revision-7"
    assert proposal["state"] == "PENDING"
    assert proposal["evidence"] == ["high CPU", "memory growth"]
    assert result.rca_id == "rca-1"
    assert result.verification_steps == ["Check memory"]
    assert result.permanent_remediation == "Fix code"
    assert result.execution_steps == [ExecutionStep(**step.model_dump()) for step in draft.execution_steps]
    assert result.verification_status is PlaybookVerificationStatus.DRAFT
    assert proposal["after"]["verification_steps"] == ["Check memory", "Inspect heap"]
    assert proposal["after"]["execution_steps"] == proposal["before"]["execution_steps"] == previous["execution_steps"]
    assert proposal["after"]["verification_status"] == proposal["before"]["verification_status"] == "VERIFIED"
    assert proposal["after"]["rca_id"] == proposal["before"]["rca_id"] == "rca-0"
    annotations = {"comparison", "library_revision", "source_engine", "source_rca_id"}
    assert proposal["before"] == {key: value for key, value in previous.items() if key not in annotations}
    assert {change["field"] for change in proposal["changes"]} == {"verification_steps", "permanent_remediation"}
    assert existing.model_dump(mode="json") == previous
    frozen = deepcopy(proposal)
    existing.verification_steps.append("later knowledge")
    existing.execution_steps[1].metric_wait["region"] = "eu-west-1"
    appraisal.verification_steps.append("late model change")
    result.execution_steps[0].commands[0] = "later incident command"
    assert proposal == frozen
    assert Playbook.model_validate_json(result.model_dump_json()).comparison == result.comparison


def test_reference_must_exist_in_current_report_not_just_historical_playbook():
    report = _make_report()
    report.evidence_list = ["[heap-42] heap keeps growing while traffic is steady"]
    existing = _make_existing(verification_steps=["[historical-only] old memory evidence"])
    rejected, _, _, _ = _run(
        [_make_hit()],
        report=report,
        details=[existing],
        appraisals=[_make_appraisal(needs_update=True, tags=["heap"], evidence=["[historical-only]"])],
    )
    assert rejected.comparison["status"] == "SEARCH_FAILED"
    accepted, _, _, _ = _run(
        [_make_hit()],
        report=report,
        details=[existing],
        appraisals=[_make_appraisal(needs_update=True, tags=["heap"], evidence=["[heap-42]"])],
    )
    assert accepted.comparison["proposal"]["evidence"] == ["[heap-42]"]


@pytest.mark.parametrize("needs_update", [False, True])
def test_no_change_and_proposal_freeze_used_baseline_and_actual_cited_report_values(needs_update):
    """Reference roles describe actual reuse and preserve observations after the source objects change."""
    report = _make_report()
    report.evidence_list = ["[heap-42] observed heap growth", "[heap-42] observed stable traffic", "not cited"]
    existing = _make_existing(source_engine="headless-codex", source_rca_id="prior", library_revision="r7")
    result, _, _, _ = _run(
        [_make_hit(playbook_id="searched-only", similarity=0.99), _make_hit()],
        details=[None, existing],
        report=report,
        appraisals=[_make_appraisal(needs_update=needs_update, tags=["new"], evidence=["[heap-42]"])],
    )
    wire = result.comparison
    assert wire["baseline"]["playbook"] == {
        key: value
        for key, value in existing.model_dump(mode="json").items()
        if key not in {"comparison", "library_revision", "source_engine", "source_rca_id"}
    }
    assert wire["baseline"]["revision"] == "r7"
    assert [item["value"] for item in wire["evidence"]] == report.evidence_list[:2]
    assert [item["source_path"] for item in wire["evidence"]] == [
        "current_report#/evidence_list/0",
        "current_report#/evidence_list/1",
    ]
    assert {item["role"] for item in wire["used_references"]} == {
        "knowledge-reuse",
        "current-runbook-input",
        "comparison-evidence",
    }
    assert [item["playbook_id"] for item in wire["used_references"] if item["role"] == "knowledge-reuse"] == [
        "existing-1"
    ]
    assert wire["inputs"]["current_report"] == report.model_dump(mode="json")
    frozen = deepcopy(wire)
    report.evidence_list.clear()
    existing.verification_steps.append("newer knowledge")
    assert wire == frozen


@pytest.mark.parametrize("field", ["applicable", "needs_update", "rationale", "evidence"])
def test_model_appraisal_requires_explicit_decision_and_evidence(field):
    output = _make_appraisal().model_dump()
    del output[field]
    with pytest.raises(ValidationError):
        PlaybookUpdateOutput.model_validate(output)


def test_whitespace_rationale_cannot_create_a_proposal():
    result, _, _, _ = _run(
        [_make_hit()], appraisals=[_make_appraisal(needs_update=True, tags=["heap"], rationale=" \n ")]
    )
    assert result.comparison["status"] == "SEARCH_FAILED"


def test_model_cannot_choose_proposal_identity_provenance_or_verification_status():
    existing = _make_existing(
        library_revision="published-revision",
        source_engine="headless-codex",
        source_rca_id="published-source",
    )
    model_values = _make_appraisal(needs_update=True, tags=["heap"]).model_dump()
    model_values.update(
        proposal_id="model-selected-id",
        playbook_id="different-playbook",
        source_engine="model-engine",
        source_rca_id="model-source",
        base_revision="model-revision",
        verification_status="VERIFIED",
    )
    result, _, _, _ = _run(
        [_make_hit()], details=[existing], appraisals=[PlaybookUpdateOutput.model_validate(model_values)]
    )
    proposal = result.comparison["proposal"]
    assert proposal["proposal_id"] != "model-selected-id"
    assert proposal["playbook_id"] == result.playbook_id == existing.playbook_id
    assert proposal["source_engine"] == "headless-codex"
    assert proposal["source_rca_id"] == "published-source"
    assert proposal["base_revision"] == "published-revision"
    assert proposal["before"]["verification_status"] == proposal["after"]["verification_status"] == "DRAFT"


def test_misidentified_detail_is_unavailable_without_model_appraisal():
    result, agent, _, _ = _run([_make_hit()], details=[_make_existing(playbook_id="another-id")])
    assert result.comparison["status"] == "SEARCH_FAILED"
    assert result.comparison["candidates"][0]["availability"] == "UNAVAILABLE"
    assert result.comparison["candidates"][0]["applicable"] is None
    assert agent.call_count == 1


def test_claimed_change_with_no_actual_knowledge_difference_is_no_change():
    existing = _make_existing(verification_steps=["Check memory", "Check memory"])
    result, _, _, _ = _run(
        [_make_hit()],
        details=[existing],
        appraisals=[_make_appraisal(needs_update=True, verification_steps=[], tags=[], temporary_mitigation=" ")],
    )
    assert result.playbook_id == "existing-1"
    assert result.comparison["status"] == "NO_CHANGE"
    assert "proposal" not in result.comparison
    assert result.verification_steps == existing.verification_steps


def test_unconfirmed_incident_never_inherits_historical_runbook():
    report = _make_report()
    report.root_cause_confirmed = False
    existing = _make_existing(
        execution_steps=[ExecutionStep(**command_step())],
        verification_status=PlaybookVerificationStatus.VERIFIED,
    )
    result, _, _, _ = _run(
        [_make_hit()], details=[existing], report=report, appraisals=[_make_appraisal(needs_update=True, tags=["heap"])]
    )
    assert result.execution_steps == []
    assert result.verification_status is PlaybookVerificationStatus.DRAFT
    assert result.comparison["proposal"]["before"]["execution_steps"]
    assert json.loads(result.model_dump_json())["comparison"]["proposal"]["after"]["execution_steps"]
