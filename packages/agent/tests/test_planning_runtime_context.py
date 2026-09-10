"""Planning context exposes existing limits without imposing a validation order."""

from unittest.mock import MagicMock

import pytest

from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ScopingResult,
)
from rca_agent.prompts.hypothesis import HYPOTHESIS_GENERATION_SYSTEM_PROMPT
from rca_agent.services import prioritization
from rca_agent.services.pipeline import select_beam
from rca_agent.services.prioritization import (
    PrioritizationOutput,
    _build_user_prompt,
    _PrioritizedItem,
    run_prioritization,
)


def _hypotheses():
    return [
        Hypothesis(
            hypothesis_id=identifier,
            description=f"Observed mechanism {identifier}",
            category=HypothesisCategory.DEPLOYMENT,
            confidence_score=0.7,
            tree_id="offline-tree",
        )
        for identifier in ("candidate", "alternative-a", "alternative-b", "alternative-c")
    ]


@pytest.mark.parametrize("beam_width, loop_limit, exit_score", [(3, 3, 0.9), (2, 4, 0.93)])
def test_planning_context_uses_configured_limits_and_current_hypothesis_states(
    monkeypatch, beam_width, loop_limit, exit_score
):
    # Alternate values test interpolation only; production settings are untouched.
    monkeypatch.setattr(prioritization, "RCA_BEAM_WIDTH", beam_width, raising=False)
    monkeypatch.setattr(prioritization, "RCA_MAX_VALIDATION_LOOPS", loop_limit, raising=False)
    monkeypatch.setattr(prioritization, "TERMINATION_CONFIDENCE_THRESHOLD", exit_score, raising=False)
    hypotheses = _hypotheses()
    hypotheses[1].status = HypothesisStatus.NEEDS_INVESTIGATION
    hypotheses[2].status = HypothesisStatus.REJECTED
    hypotheses[3].status = HypothesisStatus.CONFIRMED
    before = [hypothesis.model_dump() for hypothesis in hypotheses]

    prompt = _build_user_prompt(ScopingResult(alarm_summary="Offline symptom"), hypotheses)

    assert f"At most {beam_width} highest-ranked PENDING/NEEDS_INVESTIGATION hypotheses" in prompt
    assert f"At most {loop_limit} validation loops" in prompt
    assert f"CONFIRMED hypothesis has confidence >= {exit_score}" in prompt
    assert "before another validation loop or branching step" in prompt
    assert "no fixed hypothesis order" in prompt
    for hypothesis in hypotheses:
        assert f"status={hypothesis.status.value}" in prompt
    assert [hypothesis.model_dump() for hypothesis in hypotheses] == before


@pytest.mark.parametrize(
    "model_order",
    [
        ("candidate", "alternative-a", "alternative-b", "alternative-c"),
        ("alternative-c", "alternative-b", "alternative-a", "candidate"),
    ],
)
def test_model_can_rank_candidate_first_or_last_without_server_reordering(model_order):
    hypotheses = _hypotheses()
    before = [hypothesis.model_dump() for hypothesis in hypotheses]
    output = PrioritizationOutput(
        prioritized=[
            _PrioritizedItem(hypothesis_id=identifier, priority_rank=index + 1)
            for index, identifier in enumerate(model_order)
        ]
    )
    agent = MagicMock(return_value=MagicMock(structured_output=output))

    result = run_prioritization(ScopingResult(alarm_summary="Offline symptom"), hypotheses, agent)

    assert [item.hypothesis_id for item in result.prioritized] == list(model_order)
    assert [hypothesis.hypothesis_id for hypothesis in select_beam(hypotheses, result, 3)] == list(model_order[:3])
    assert [hypothesis.model_dump() for hypothesis in hypotheses] == before
    agent.assert_called_once()


def test_generator_clarifies_causal_relationships_without_banning_multiple_factors():
    """Require distinct testable claims while allowing separately validated cofactors."""
    assert "alternative causal explanations from compatible contributing factors" in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    assert "state whether they compete or could hold together" in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    assert "retain separately testable cofactors" in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    assert "one falsifiable causal claim" in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    assert "a final explanation may combine separately validated facts" in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    assert "is preferred over collapsing everything into one factor" not in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    assert "Generate exactly 3 to 5 hypotheses, ordered by likelihood." in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
