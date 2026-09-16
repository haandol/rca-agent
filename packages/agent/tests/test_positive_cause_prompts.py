"""Guard the causal question and the captured real counterexample without changing server decisions."""

import json
from pathlib import Path

from rca_agent.config.settings import CONFIRMATION_THRESHOLD, TERMINATION_CONFIDENCE_THRESHOLD
from rca_agent.ports.dto.models import Hypothesis, HypothesisCategory
from rca_agent.prompts.hypothesis import HYPOTHESIS_GENERATION_SYSTEM_PROMPT
from rca_agent.prompts.validation import VALIDATION_SYSTEM_PROMPT
from rca_agent.services.validation import _build_user_prompt


def capture():
    """Read frozen observations and preserve old full reasoning strictly as audit data."""
    return json.loads((Path(__file__).parent / "fixtures/root-cause-positive-mechanism-live04.json").read_text())


def test_prompt_contract_distinguishes_cause_from_truth_and_preserves_multicausal_reasoning():
    """The repair targets the meaning of confirmation, not thresholds or categories of contributing causes."""
    generation = HYPOTHESIS_GENERATION_SYSTEM_PROMPT
    validation = VALIDATION_SYSTEM_PROMPT
    assert "four allowlisted injected fault conditions" not in generation
    assert "positive, falsifiable causal mechanism" in generation
    assert "5 Whys" in generation and "compatible contributing factors separately" in generation
    assert "actual causal contribution" in generation
    assert "reject it as a root cause candidate even when its observations are correct" in validation
    assert "Do not silently rewrite it into a different causal hypothesis" in validation
    assert "Safeguards and amplifiers are not categorically excluded" in validation
    assert "improves diagnosis or strengthens confidence" in validation
    assert "rejects the candidate AS AN INCIDENT CAUSE" in validation
    assert "Historical similarity" in generation and "current contradictory" in generation
    assert "Past incidents are investigation clues only" in validation
    assert "CONFIRMED (>=0.8), REJECTED (<=0.3)" in validation
    assert CONFIRMATION_THRESHOLD == 0.8 and TERMINATION_CONFIDENCE_THRESHOLD == 0.9
    for name in ("DB_CONNECTION_LEAK", "HIGH_CPU", "HIGH_MEMORY", "SLOW_QUERY", "UNSUPPORTED"):
        assert name in generation and name in validation


def test_real_exclusion_failure_keeps_full_original_reasoning_and_rpc_warning():
    """The real prior verdict admitted its exclusion-only scope; preserve that evidence beyond 500 chars."""
    data = capture()
    h4 = next(h for h in data["hypotheses"] if h["hypothesis_id"].startswith("95a0"))
    old = h4["original_validation_record"]
    assert old["status"] == "CONFIRMED" and old["confidence_score"] == 0.93
    assert len(old["reasoning"]) > 500 and "배제적 검증" in old["reasoning"]
    h1 = next(h for h in data["hypotheses"] if h["hypothesis_id"].startswith("04dec"))
    assert h1["original_validation_record"]["status"] == "NEEDS_INVESTIGATION"
    assert h1["original_validation_record"]["confidence_score"] == 0.93
    assert "422" in json.dumps(data["model_input"], ensure_ascii=False)


def test_real_model_inputs_have_observations_and_candidate_but_no_expected_or_previous_verdict():
    """Both candidates get the same captured observations; previous model judgments cannot steer the rerun."""
    data = capture()
    evidence = json.dumps(data["model_input"], ensure_ascii=False)
    suffixes = []
    for item in data["hypotheses"]:
        hypothesis = Hypothesis(
            description=item["description"], category=HypothesisCategory(item["category"]), confidence_score=0.5
        )
        prompt = _build_user_prompt(hypothesis, evidence, None)
        assert item["description"] in prompt and evidence in prompt
        assert item["original_validation_record"]["reasoning"] not in prompt
        assert "original_validation_record" not in prompt
        suffixes.append(prompt.split("## Evidence\n", 1)[1])
    assert suffixes[0] == suffixes[1]
