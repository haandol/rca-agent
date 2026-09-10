import time

from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    TerminationReason,
    ValidationJudgment,
)
from rca_agent.services.termination import check_termination


def _make_hypothesis(hid="h-1", depth=0, status=HypothesisStatus.NEEDS_INVESTIGATION) -> Hypothesis:
    """Represent a tree node after the pipeline has applied its validation judgment."""
    return Hypothesis(
        hypothesis_id=hid,
        description="Test",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.5,
        tree_id="tree-1",
        depth=depth,
        status=status,
    )


def _make_judgment(hid="h-1", status=HypothesisStatus.PENDING, confidence=0.5) -> ValidationJudgment:
    return ValidationJudgment(hypothesis_id=hid, status=status, confidence_score=confidence)


class TestCheckTermination:
    def test_confirmed_high_confidence(self):
        """A valid current confirmation still terminates immediately above 0.9."""
        j = _make_judgment(status=HypothesisStatus.CONFIRMED, confidence=0.95)
        h = _make_hypothesis(status=j.status)

        decision = check_termination(
            judgments=[j], hypotheses=[h], start_time=time.monotonic(), validation_loop_count=1
        )

        assert decision.should_terminate
        assert decision.reason == TerminationReason.CONFIRMED
        assert decision.best_hypothesis is not None

    def test_confirmed_below_threshold_continues(self):
        """A current 0.85 confirmation does not satisfy the 0.9 exit threshold."""
        j = _make_judgment(status=HypothesisStatus.CONFIRMED, confidence=0.85)
        h = _make_hypothesis(status=j.status)

        decision = check_termination(
            judgments=[j], hypotheses=[h], start_time=time.monotonic(), validation_loop_count=1
        )

        assert not decision.should_terminate

    def test_time_budget_exceeded(self):
        j = _make_judgment(status=HypothesisStatus.NEEDS_INVESTIGATION, confidence=0.5)
        h = _make_hypothesis()

        decision = check_termination(
            judgments=[j],
            hypotheses=[h],
            start_time=time.monotonic() - 1300,
            validation_loop_count=1,
            time_budget=1200,
        )

        assert decision.should_terminate
        assert decision.reason == TerminationReason.TIME_BUDGET

    def test_max_depth_reached(self):
        j = _make_judgment(status=HypothesisStatus.NEEDS_INVESTIGATION, confidence=0.5)
        h = _make_hypothesis(depth=3)

        decision = check_termination(
            judgments=[j], hypotheses=[h], start_time=time.monotonic(), validation_loop_count=1, max_depth=3
        )

        assert decision.should_terminate
        assert decision.reason == TerminationReason.MAX_DEPTH

    def test_third_loop_reaches_the_maximum(self):
        j = _make_judgment(status=HypothesisStatus.NEEDS_INVESTIGATION, confidence=0.5)
        h = _make_hypothesis()

        decision = check_termination(
            judgments=[j], hypotheses=[h], start_time=time.monotonic(), validation_loop_count=3, max_loops=3
        )

        assert decision.should_terminate
        assert decision.reason == TerminationReason.MAX_LOOPS

    def test_all_rejected_does_not_terminate(self):
        """A directly rejected current node still permits the regeneration path."""
        j = _make_judgment(status=HypothesisStatus.REJECTED, confidence=0.1)
        h = _make_hypothesis(status=j.status)

        decision = check_termination(
            judgments=[j], hypotheses=[h], start_time=time.monotonic(), validation_loop_count=1
        )

        assert not decision.should_terminate

    def test_no_termination(self):
        j = _make_judgment(status=HypothesisStatus.NEEDS_INVESTIGATION, confidence=0.5)
        h = _make_hypothesis()

        decision = check_termination(
            judgments=[j], hypotheses=[h], start_time=time.monotonic(), validation_loop_count=1
        )

        assert not decision.should_terminate
        assert decision.reason is None

    def test_best_hypothesis_on_forced_stop(self):
        j1 = _make_judgment("h-1", HypothesisStatus.NEEDS_INVESTIGATION, 0.6)
        j2 = _make_judgment("h-2", HypothesisStatus.NEEDS_INVESTIGATION, 0.4)
        h1 = _make_hypothesis("h-1")
        h2 = _make_hypothesis("h-2")

        decision = check_termination(
            judgments=[j1, j2],
            hypotheses=[h1, h2],
            start_time=time.monotonic() - 1300,
            validation_loop_count=1,
            time_budget=1200,
        )

        assert decision.best_hypothesis is not None
        assert decision.best_hypothesis.hypothesis_id == "h-1"
