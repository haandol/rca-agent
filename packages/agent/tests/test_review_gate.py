from __future__ import annotations

from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ValidationJudgment,
)
from rca_agent.services.review_gate import run_review_gate


def _hypo(
    hid: str,
    *,
    description: str,
    category: HypothesisCategory = HypothesisCategory.DEPENDENCY,
    status: HypothesisStatus = HypothesisStatus.PENDING,
    confidence: float = 0.5,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        description=description,
        category=category,
        confidence_score=confidence,
        status=status,
    )


def _judgment(hid: str, status: HypothesisStatus, confidence: float) -> ValidationJudgment:
    return ValidationJudgment(hypothesis_id=hid, status=status, confidence_score=confidence, reasoning="")


def test_gate_passes_when_no_accepted():
    hypotheses = [_hypo("a", description="foo")]
    result = run_review_gate(hypotheses, [], consecutive_blocked_loops=0)
    assert result.early_exit is False
    assert result.expansion_blocked is False
    assert result.reason == "no_accepted"


def test_gate_early_exit_on_high_confidence():
    hypotheses = [
        _hypo(
            "a",
            description="Healthcare 앱 커넥션 누수",
            status=HypothesisStatus.CONFIRMED,
            confidence=0.95,
        )
    ]
    judgments = [_judgment("a", HypothesisStatus.CONFIRMED, 0.95)]
    result = run_review_gate(hypotheses, judgments, consecutive_blocked_loops=0)
    assert result.early_exit is True
    assert result.expansion_blocked is False
    assert result.accepted_max_confidence >= 0.9


def test_gate_expansion_blocked_in_grace_range():
    hypotheses = [
        _hypo(
            "a",
            description="Healthcare 앱 커넥션 누수",
            status=HypothesisStatus.CONFIRMED,
            confidence=0.82,
        )
    ]
    judgments = [_judgment("a", HypothesisStatus.CONFIRMED, 0.82)]
    result = run_review_gate(hypotheses, judgments, consecutive_blocked_loops=0)
    assert result.early_exit is False
    assert result.expansion_blocked is True


def test_gate_promotes_to_early_exit_after_grace_loops():
    hypotheses = [
        _hypo(
            "a",
            description="foo",
            status=HypothesisStatus.CONFIRMED,
            confidence=0.82,
        )
    ]
    judgments = [_judgment("a", HypothesisStatus.CONFIRMED, 0.82)]
    result = run_review_gate(hypotheses, judgments, consecutive_blocked_loops=2)
    assert result.early_exit is True
    assert "grace_loops_exhausted" in result.reason


def test_gate_auto_rejects_similar_pending_hypothesis():
    accepted = _hypo(
        "a",
        description="Healthcare 앱 커넥션 누수",
        category=HypothesisCategory.DEPENDENCY,
        status=HypothesisStatus.CONFIRMED,
        confidence=0.82,
    )
    similar = _hypo(
        "b",
        description="Healthcare 앱 커넥션 누수 세부",
        category=HypothesisCategory.DEPENDENCY,
        status=HypothesisStatus.PENDING,
    )
    unrelated = _hypo(
        "c",
        description="트래픽 급증 기인 CPU 증가",
        category=HypothesisCategory.TRAFFIC,
        status=HypothesisStatus.PENDING,
    )
    hypotheses = [accepted, similar, unrelated]
    judgments = [_judgment("a", HypothesisStatus.CONFIRMED, 0.82)]
    result = run_review_gate(hypotheses, judgments, consecutive_blocked_loops=0)
    assert "b" in result.auto_rejected_ids
    assert "c" not in result.auto_rejected_ids
    assert similar.status == HypothesisStatus.REJECTED
    assert unrelated.status == HypothesisStatus.PENDING


def test_gate_does_not_touch_confirmed_status():
    accepted = _hypo(
        "a",
        description="foo bar baz",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.82,
    )
    hypotheses = [accepted]
    judgments = [_judgment("a", HypothesisStatus.CONFIRMED, 0.82)]
    result = run_review_gate(hypotheses, judgments, consecutive_blocked_loops=0)
    assert accepted.status == HypothesisStatus.CONFIRMED
    assert result.auto_rejected_ids == []
