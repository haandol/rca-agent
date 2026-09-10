"""Beam rejection is not global exhaustion; all collaborators are offline fakes."""

import time
from unittest.mock import MagicMock, patch

import pytest

from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    PrioritizationResult,
    PrioritizedHypothesis,
    ScopingResult,
    TerminationReason,
    ValidationJudgment,
    ValidationResult,
)
from rca_agent.services.pipeline import (
    PipelineOrchestrator,
    RunContext,
    ValidationLoopState,
)
from rca_agent.services.review_gate import run_review_gate


def _hypothesis(identifier, status=HypothesisStatus.PENDING, confidence=0.5):
    return Hypothesis(
        hypothesis_id=identifier,
        description=f"Independent explanation {identifier}",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=confidence,
        status=status,
        tree_id="original-tree",
    )


def _rejected_beam():
    hypotheses = [_hypothesis(f"alternative-{index}", HypothesisStatus.REJECTED, 0.1) for index in range(3)]
    judgments = [
        ValidationJudgment(
            hypothesis_id=hypothesis.hypothesis_id,
            status=HypothesisStatus.REJECTED,
            confidence_score=0.1,
            reasoning=f"Evidence contradicts {hypothesis.hypothesis_id}",
            evidence_summary=[f"evidence-{hypothesis.hypothesis_id}"],
        )
        for hypothesis in hypotheses
    ]
    return hypotheses, ValidationResult(tree_id="original-tree", judgments=judgments, all_rejected=True)


def _run():
    return RunContext(
        rca_id="offline-rca",
        claim_token="offline-claim",
        attempt=1,
        trace=MagicMock(),
        start_time=time.monotonic(),
    )


def test_three_rejected_beam_members_preserve_pending_root_and_validate_it_next_loop():
    hypotheses = [_hypothesis(f"alternative-{index}") for index in range(3)]
    root = _hypothesis("pending-root")
    hypotheses.append(root)
    original_ids = [hypothesis.hypothesis_id for hypothesis in hypotheses]
    selected_beams = []
    run = _run()
    container = MagicMock()
    orchestrator = PipelineOrchestrator(container, precollected_evidence="Offline observations")
    priority = PrioritizationResult(
        tree_id="original-tree",
        prioritized=[
            PrioritizedHypothesis(hypothesis_id=identifier, priority_rank=index + 1)
            for index, identifier in enumerate(original_ids)
        ],
    )

    def validate(selected, *_args, **_kwargs):
        selected_beams.append([hypothesis.hypothesis_id for hypothesis in selected])
        if len(selected_beams) == 1:
            assert root.status == HypothesisStatus.PENDING
            return _rejected_beam()[1]
        assert selected == [root]
        assert root.status == HypothesisStatus.PENDING
        return ValidationResult(
            tree_id="original-tree",
            judgments=[
                ValidationJudgment(
                    hypothesis_id=root.hypothesis_id,
                    status=HypothesisStatus.CONFIRMED,
                    confidence_score=0.95,
                    reasoning="Independent evidence confirms the remaining root",
                    evidence_summary=["root-evidence"],
                )
            ],
        )

    with (
        patch("rca_agent.services.pipeline.run_prioritization", return_value=priority),
        patch("rca_agent.services.pipeline.run_validation", side_effect=validate),
        patch(
            "rca_agent.services.pipeline.run_hypothesis_generation",
            side_effect=AssertionError("A rejected beam must not discard an unselected root"),
        ) as regenerate,
        patch("rca_agent.services.pipeline.run_branching") as branch,
    ):
        state = orchestrator._run_validation_loop(
            MagicMock(alarm_name="offline-alarm"),
            ScopingResult(alarm_summary="Offline symptom"),
            hypotheses,
            run,
        )

    assert selected_beams == [original_ids[:3], [root.hypothesis_id]]
    assert state.loop_count == 2
    assert state.regeneration_count == 0
    assert [hypothesis.hypothesis_id for hypothesis in state.hypotheses] == original_ids
    assert state.hypotheses[-1] is root
    assert root.status == HypothesisStatus.CONFIRMED
    assert state.termination.reason == TerminationReason.CONFIRMED
    assert state.termination.best_hypothesis.hypothesis_id == root.hypothesis_id
    assert all(hypothesis.status == HypothesisStatus.REJECTED for hypothesis in hypotheses[:3])
    assert not any(
        call.args[0] == root.hypothesis_id and call.kwargs["status"] == "REJECTED"
        for call in run.trace.update_hypothesis_status.call_args_list
    )
    regenerate.assert_not_called()
    branch.assert_not_called()
    run.trace.put_hypotheses.assert_not_called()


@pytest.mark.parametrize("remaining_status", [HypothesisStatus.PENDING, HypothesisStatus.NEEDS_INVESTIGATION])
def test_rejected_beam_does_not_mutate_unselected_unresolved_hypothesis(remaining_status):
    hypotheses, validation = _rejected_beam()
    remaining = _hypothesis("unselected", remaining_status)
    state = ValidationLoopState(
        hypotheses=[*hypotheses, remaining],
        all_judgments=validation.judgments,
        loop_count=1,
    )
    run = _run()
    container = MagicMock()
    orchestrator = PipelineOrchestrator(container)
    gate = run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0)
    original = remaining.model_dump()
    with patch("rca_agent.services.pipeline.run_hypothesis_generation") as regenerate:
        action = orchestrator._maybe_regenerate(
            state, ScopingResult(alarm_summary="Offline symptom"), validation, gate, run, MagicMock()
        )
    assert action.name == "PROCEED"
    assert remaining.model_dump() == original
    assert state.regeneration_count == 0
    assert orchestrator._loop_branching(state, gate, run.trace, MagicMock())
    regenerate.assert_not_called()
    container.session_store.update_state.assert_not_called()
    run.trace.update_hypothesis_status.assert_not_called()


def test_globally_rejected_hypotheses_still_regenerate():
    hypotheses, validation = _rejected_beam()
    state = ValidationLoopState(hypotheses=hypotheses, all_judgments=validation.judgments, loop_count=1)
    new_hypotheses = [_hypothesis(f"new-{index}") for index in range(3)]
    for hypothesis in new_hypotheses:
        hypothesis.tree_id = "new-tree"
    run = _run()
    container = MagicMock()
    gate = run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0)
    with patch(
        "rca_agent.services.pipeline.run_hypothesis_generation",
        return_value=MagicMock(hypotheses=new_hypotheses),
    ) as regenerate:
        action = PipelineOrchestrator(container)._maybe_regenerate(
            state, ScopingResult(alarm_summary="Offline symptom"), validation, gate, run, MagicMock()
        )
    assert action.name == "CONTINUE"
    assert state.regeneration_count == 1
    assert state.hypotheses == new_hypotheses
    assert all(hypothesis.status == HypothesisStatus.REJECTED for hypothesis in hypotheses)
    regenerate.assert_called_once()
    assert len(regenerate.call_args.kwargs["rejection_feedback"]) == 3
    container.session_store.update_state.assert_called_once()
    run.trace.put_hypotheses.assert_called_once_with(new_hypotheses)


def test_expansion_blocked_rejected_beam_retains_existing_continue_behavior():
    hypotheses, validation = _rejected_beam()
    accepted = _hypothesis("accepted", HypothesisStatus.CONFIRMED, 0.85)
    state = ValidationLoopState(
        hypotheses=[*hypotheses, accepted],
        all_judgments=validation.judgments,
        loop_count=2,
        consecutive_blocked_loops=1,
    )
    run = _run()
    container = MagicMock()
    gate = run_review_gate(state.hypotheses, state.all_judgments, consecutive_blocked_loops=0)
    assert gate.expansion_blocked
    assert not gate.early_exit
    with patch("rca_agent.services.pipeline.run_hypothesis_generation") as regenerate:
        action = PipelineOrchestrator(container)._maybe_regenerate(
            state, ScopingResult(alarm_summary="Offline symptom"), validation, gate, run, MagicMock()
        )
    assert action.name == "CONTINUE"
    assert state.regeneration_count == 0
    assert accepted.status == HypothesisStatus.CONFIRMED
    assert accepted.confidence_score == 0.85
    assert state.consecutive_blocked_loops == 1
    assert state.timeline == ["Loop 2: regeneration skipped (expansion blocked)"]
    run.trace.end_span.assert_called_once()
    assert run.trace.end_span.call_args.kwargs["output_summary"] == "expansion blocked, regeneration skipped"
    regenerate.assert_not_called()
    container.session_store.update_state.assert_not_called()
