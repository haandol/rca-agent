from unittest.mock import MagicMock

from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
)
from rca_agent.services.validation import (
    ValidationOutput,
    _classify_status,
    _JudgmentItem,
    run_validation,
    validate_hypothesis,
)


def _make_hypothesis(hid="h-1", confidence=0.5, required_evidence=None) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        description="Test hypothesis",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=confidence,
        required_evidence=required_evidence or [],
        tree_id="tree-1",
    )


def _make_mock_agent(status: HypothesisStatus, confidence: float, reasoning: str = "test") -> MagicMock:
    output = ValidationOutput(
        judgment=_JudgmentItem(
            status=status,
            confidence_score=confidence,
            reasoning=reasoning,
            evidence_summary=["evidence-1"],
        )
    )
    mock_result = MagicMock()
    mock_result.structured_output = output
    agent = MagicMock()
    agent.return_value = mock_result
    return agent


class TestClassifyStatus:
    def test_confirmed(self):
        assert _classify_status(0.85) == HypothesisStatus.CONFIRMED

    def test_rejected(self):
        assert _classify_status(0.2) == HypothesisStatus.REJECTED

    def test_needs_investigation(self):
        assert _classify_status(0.5) == HypothesisStatus.NEEDS_INVESTIGATION

    def test_boundary_confirmed(self):
        assert _classify_status(0.8) == HypothesisStatus.CONFIRMED

    def test_boundary_rejected(self):
        assert _classify_status(0.3) == HypothesisStatus.REJECTED


class TestValidateHypothesis:
    def test_returns_judgment(self):
        h = _make_hypothesis()
        agent = _make_mock_agent(HypothesisStatus.CONFIRMED, 0.9, "Strong evidence")

        judgment = validate_hypothesis(h, "CPU spiked after deploy", agent)

        assert judgment.hypothesis_id == "h-1"
        assert judgment.status == HypothesisStatus.CONFIRMED
        assert judgment.confidence_score == 0.9

    def test_uses_structured_output(self):
        h = _make_hypothesis()
        agent = _make_mock_agent(HypothesisStatus.CONFIRMED, 0.9)

        validate_hypothesis(h, "evidence", agent)

        _, kwargs = agent.call_args
        assert kwargs["structured_output_model"] is ValidationOutput

    def test_reclassifies_status_by_score(self):
        h = _make_hypothesis()
        agent = _make_mock_agent(HypothesisStatus.CONFIRMED, 0.5)

        judgment = validate_hypothesis(h, "evidence", agent)

        assert judgment.status == HypothesisStatus.NEEDS_INVESTIGATION

    def test_timeout_returns_needs_investigation(self):
        h = _make_hypothesis()
        agent = MagicMock(side_effect=RuntimeError("timeout"))

        judgment = validate_hypothesis(h, "evidence", agent)

        assert judgment.status == HypothesisStatus.NEEDS_INVESTIGATION
        assert judgment.confidence_score == h.confidence_score

    def test_caps_confirmed_when_evidence_failed_with_required_evidence(self):
        h = _make_hypothesis(required_evidence=["deployment history", "logs"])
        agent = _make_mock_agent(HypothesisStatus.CONFIRMED, 0.9, "Strong evidence")

        judgment = validate_hypothesis(h, "Evidence collection timed out or failed.", agent, evidence_failed=True)

        assert judgment.status == HypothesisStatus.NEEDS_INVESTIGATION
        assert judgment.confidence_score == 0.9

    def test_allows_confirmed_when_evidence_failed_but_no_required_evidence(self):
        h = _make_hypothesis(required_evidence=[])
        agent = _make_mock_agent(HypothesisStatus.CONFIRMED, 0.9, "Strong evidence")

        judgment = validate_hypothesis(h, "Evidence collection timed out or failed.", agent, evidence_failed=True)

        assert judgment.status == HypothesisStatus.CONFIRMED

    def test_no_cap_when_evidence_succeeded(self):
        h = _make_hypothesis(required_evidence=["deployment history"])
        agent = _make_mock_agent(HypothesisStatus.CONFIRMED, 0.9, "Strong evidence")

        judgment = validate_hypothesis(h, "CPU spiked after deploy", agent, evidence_failed=False)

        assert judgment.status == HypothesisStatus.CONFIRMED


class TestRunValidation:
    def test_validates_all_hypotheses(self):
        hyps = [_make_hypothesis("h-1"), _make_hypothesis("h-2")]
        agent = _make_mock_agent(HypothesisStatus.CONFIRMED, 0.85)

        result = run_validation(hyps, {"h-1": "evidence-1", "h-2": "evidence-2"}, agent)

        assert len(result.judgments) == 2
        assert not result.all_rejected

    def test_detects_all_rejected(self):
        hyps = [_make_hypothesis("h-1")]
        agent = _make_mock_agent(HypothesisStatus.REJECTED, 0.1)

        result = run_validation(hyps, {}, agent)

        assert result.all_rejected

    def test_caps_confirmed_for_failed_evidence_ids(self):
        hyps = [_make_hypothesis("h-1", required_evidence=["logs"])]
        agent = _make_mock_agent(HypothesisStatus.CONFIRMED, 0.9)

        result = run_validation(hyps, {"h-1": "timed out"}, agent, evidence_failed_ids={"h-1"})

        assert result.judgments[0].status == HypothesisStatus.NEEDS_INVESTIGATION
