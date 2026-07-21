from __future__ import annotations

from unittest.mock import patch

import pytest

from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ValidationJudgment,
)
from rca_agent.services.review_gate import run_review_gate
from rca_agent.services.termination import check_termination
from rca_agent.services.validation import _classify_status


def _hypothesis(
    hypothesis_id: str,
    *,
    status: HypothesisStatus = HypothesisStatus.PENDING,
    confidence: float = 0.5,
    description: str = "database connection pool exhaustion",
    category: HypothesisCategory = HypothesisCategory.DEPENDENCY,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        description=description,
        category=category,
        confidence_score=confidence,
        status=status,
        tree_id="tree-contract",
    )


def _judgment(
    hypothesis_id: str,
    *,
    status: HypothesisStatus,
    confidence: float,
) -> ValidationJudgment:
    return ValidationJudgment(
        hypothesis_id=hypothesis_id,
        status=status,
        confidence_score=confidence,
    )


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, HypothesisStatus.REJECTED),
        (0.3, HypothesisStatus.REJECTED),
        (0.300001, HypothesisStatus.NEEDS_INVESTIGATION),
        (0.799999, HypothesisStatus.NEEDS_INVESTIGATION),
        (0.8, HypothesisStatus.CONFIRMED),
        (1.0, HypothesisStatus.CONFIRMED),
    ],
)
def test_validation_score_boundaries_are_deterministic(score, expected):
    assert _classify_status(score) == expected


def test_review_gate_uses_latest_judgment_for_each_accepted_hypothesis():
    hypothesis = _hypothesis("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.81)
    judgments = [
        _judgment("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.82),
        _judgment("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.94),
    ]

    result = run_review_gate([hypothesis], judgments, consecutive_blocked_loops=0)

    assert result.early_exit is True
    assert result.accepted_max_confidence == 0.94


def test_review_gate_falls_back_to_hypothesis_confidence_without_judgment():
    hypothesis = _hypothesis("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.91)

    result = run_review_gate([hypothesis], [], consecutive_blocked_loops=0)

    assert result.early_exit is True
    assert result.accepted_max_confidence == 0.91


def test_review_gate_uses_maximum_across_multiple_accepted_hypotheses():
    low = _hypothesis("low", status=HypothesisStatus.CONFIRMED, confidence=0.82)
    high = _hypothesis("high", status=HypothesisStatus.CONFIRMED, confidence=0.91)

    result = run_review_gate([low, high], [], consecutive_blocked_loops=0)

    assert result.early_exit is True
    assert result.accepted_max_confidence == 0.91


def test_review_gate_does_not_auto_reject_same_text_from_different_category():
    accepted = _hypothesis("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.82)
    candidate = _hypothesis(
        "candidate",
        category=HypothesisCategory.TRAFFIC,
        description=accepted.description,
    )

    result = run_review_gate(
        [accepted, candidate],
        [_judgment("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.82)],
        consecutive_blocked_loops=0,
    )

    assert result.expansion_blocked is True
    assert result.auto_rejected_ids == []
    assert candidate.status == HypothesisStatus.PENDING


def test_review_gate_does_not_mutate_already_terminal_hypotheses():
    accepted = _hypothesis("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.82)
    rejected = _hypothesis("rejected", status=HypothesisStatus.REJECTED, description=accepted.description)
    closed = _hypothesis("closed", status=HypothesisStatus.CLOSED, description=accepted.description)

    result = run_review_gate(
        [accepted, rejected, closed],
        [_judgment("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.82)],
        consecutive_blocked_loops=0,
    )

    assert result.auto_rejected_ids == []
    assert rejected.status == HypothesisStatus.REJECTED
    assert closed.status == HypothesisStatus.CLOSED


def test_confirmed_termination_has_precedence_over_expired_time_budget():
    hypothesis = _hypothesis("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.95)
    judgment = _judgment("accepted", status=HypothesisStatus.CONFIRMED, confidence=0.95)

    with patch("rca_agent.services.termination.time.monotonic", return_value=10_000.0):
        result = check_termination(
            judgments=[judgment],
            hypotheses=[hypothesis],
            start_time=0.0,
            validation_loop_count=99,
            time_budget=1,
            max_loops=1,
        )

    assert result.should_terminate is True
    assert result.reason is not None
    assert result.reason.value == "CONFIRMED"
    assert result.best_hypothesis is hypothesis


@pytest.mark.parametrize(
    ("now", "should_terminate"),
    [
        (109.999, False),
        (110.0, True),
        (110.001, True),
    ],
)
def test_time_budget_uses_monotonic_elapsed_boundary(now, should_terminate):
    hypothesis = _hypothesis("candidate")
    judgment = _judgment(
        "candidate",
        status=HypothesisStatus.NEEDS_INVESTIGATION,
        confidence=0.6,
    )

    with patch("rca_agent.services.termination.time.monotonic", return_value=now):
        result = check_termination(
            judgments=[judgment],
            hypotheses=[hypothesis],
            start_time=100.0,
            validation_loop_count=1,
            time_budget=10,
        )

    assert result.should_terminate is should_terminate
