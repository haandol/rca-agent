"""Keep the captured incident intact and exercise claim scope without supplying expected answers."""

import json
import os
from pathlib import Path

import pytest

from rca_agent.ports.dto.models import Hypothesis, HypothesisCategory, HypothesisStatus
from rca_agent.prompts.hypothesis import HYPOTHESIS_GENERATION_SYSTEM_PROMPT
from rca_agent.prompts.validation import VALIDATION_SYSTEM_PROMPT
from rca_agent.services.validation import _build_user_prompt


def capture():
    """Read immutable observations separately from original judgments and reviewer-authored candidates."""
    return json.loads((Path(__file__).parent / "fixtures/cause-purity-live10.json").read_text())


def candidate_input(candidate, data):
    """Give each candidate identical captured observations with no outcome labels or old judgments."""
    hypothesis = Hypothesis(
        description=candidate["description"], category=HypothesisCategory.DEPLOYMENT, confidence_score=0.5
    )
    return hypothesis, json.dumps(data["model_evidence"], ensure_ascii=False)


def test_claim_scope_contract_requires_evidence_for_upstream_assertions():
    """Preserve deeper causal inquiry while preventing a partial match from confirming a composite claim."""
    assert "title and description at the same causal scope" in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    assert "positive evidence of the intended action" in HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    assert "entire stated causal claim" in VALIDATION_SYSTEM_PROMPT
    assert "do not confirm the composite candidate by silently narrowing it" in VALIDATION_SYSTEM_PROMPT
    assert "does not exclude deeper 5 Whys or compatible cofactors" in VALIDATION_SYSTEM_PROMPT


def test_frozen_case10_inputs_exclude_previous_judgment_and_expected_outcome():
    """Retain the historical overclaim as audit evidence without leaking it into fresh validation."""
    data = capture()
    assert data["original_hypothesis_api_record"]["status"] == "CONFIRMED"
    assert "마이그레이션" in data["original_hypothesis_api_record"]["title"]
    evidence_inputs = []
    for candidate in data["candidates"]:
        hypothesis, evidence = candidate_input(candidate, data)
        prompt = _build_user_prompt(hypothesis, evidence, None)
        assert "expected_confirmed" not in prompt
        assert "original_hypothesis_api_record" not in prompt
        assert data["original_hypothesis_api_record"]["judgmentReasoning"] not in prompt
        evidence_inputs.append(evidence)
    assert evidence_inputs[0] == evidence_inputs[1]
    assert "42703" in evidence_inputs[0]
    assert "sampled_at" in evidence_inputs[0] and "timestamp" in evidence_inputs[0]


@pytest.mark.skipif(os.getenv("RUN_LIVE10_CAUSE_MODEL") != "1", reason="Explicit read-only model probe opt-in")
@pytest.mark.parametrize("candidate_index", [0, 1])
def test_actual_model_distinguishes_observed_mechanism_from_unproven_extension(candidate_index):
    """Use the production validation path; never write to an RCA or alter thresholds and call budgets."""
    from rca_agent.agent_factory import create_validation_agent
    from rca_agent.services.validation import validate_hypothesis

    data = capture()
    candidate = data["candidates"][candidate_index]
    hypothesis, evidence = candidate_input(candidate, data)
    agent = create_validation_agent()
    try:
        judgment = validate_hypothesis(hypothesis, evidence, agent)
    finally:
        agent.model.client.close()
    print(
        json.dumps(
            {"case": candidate["name"], "origin": candidate["origin"], "judgment": judgment.model_dump(mode="json")},
            ensure_ascii=False,
        )
    )
    assert judgment.reasoning != "Validation timed out or failed — preserving for further investigation."
    assert (judgment.status == HypothesisStatus.CONFIRMED) is candidate["expected_confirmed"]
    assert bool(judgment.evidence_summary)
