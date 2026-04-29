"""Accepted Review Gate — ADR agent/0002.

매 검증 루프 진입 직전 실행되는 순수 로직 게이트.
채택(CONFIRMED) 가설이 있을 때 추가 탐색이 필요한지 판정하여
루프가 동일 원인 영역에서 폭주하는 현상을 차단한다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from rca_agent.config.settings import (
    RCA_ACCEPTED_SIMILARITY_THRESHOLD,
    RCA_EXPANSION_BLOCKED_GRACE_LOOPS,
    TERMINATION_CONFIDENCE_THRESHOLD,
)
from rca_agent.ports.dto.models import Hypothesis, HypothesisStatus, ValidationJudgment

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "및",
    "인한",
    "으로",
    "의해",
    "때문",
    "대한",
    "관련",
    "문제",
    "원인",
    "장애",
    "발생",
    "the",
    "and",
    "for",
    "with",
    "from",
    "due",
    "to",
    "is",
    "of",
    "a",
    "an",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣_]+")


@dataclass(frozen=True)
class ReviewGateResult:
    early_exit: bool
    expansion_blocked: bool
    reason: str
    accepted_max_confidence: float
    auto_rejected_ids: list[str]


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    tokens = {t.lower() for t in _TOKEN_RE.findall(text)}
    return tokens - _STOPWORDS


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _collect_accepted(
    hypotheses: list[Hypothesis],
    judgments: list[ValidationJudgment],
) -> tuple[list[Hypothesis], float]:
    """Return accepted hypotheses and max confidence from judgments."""
    # latest judgment per hypothesis
    latest: dict[str, ValidationJudgment] = {}
    for j in judgments:
        latest[j.hypothesis_id] = j

    accepted: list[Hypothesis] = []
    max_conf = 0.0
    for h in hypotheses:
        if h.status != HypothesisStatus.CONFIRMED:
            continue
        accepted.append(h)
        j = latest.get(h.hypothesis_id)
        score = j.confidence_score if j is not None else h.confidence_score
        max_conf = max(max_conf, score)
    return accepted, max_conf


def run_review_gate(
    hypotheses: list[Hypothesis],
    judgments: list[ValidationJudgment],
    *,
    consecutive_blocked_loops: int,
) -> ReviewGateResult:
    """Evaluate the Accepted Review Gate.

    - max accepted confidence >= TERMINATION_CONFIDENCE_THRESHOLD (0.9)
        → early_exit
    - CONFIRMATION_THRESHOLD (0.8) <= max < 0.9
        → expansion_blocked; after RCA_EXPANSION_BLOCKED_GRACE_LOOPS consecutive
          blocked loops without crossing 0.9, promote to early_exit
    - otherwise → pass-through
    Also auto-rejects PENDING/NEEDS_INVESTIGATION hypotheses that are highly
    similar to an accepted one (same category + Jaccard >= threshold).
    """
    accepted, max_conf = _collect_accepted(hypotheses, judgments)

    if not accepted:
        return ReviewGateResult(
            early_exit=False,
            expansion_blocked=False,
            reason="no_accepted",
            accepted_max_confidence=0.0,
            auto_rejected_ids=[],
        )

    if max_conf >= TERMINATION_CONFIDENCE_THRESHOLD:
        return ReviewGateResult(
            early_exit=True,
            expansion_blocked=False,
            reason=f"accepted_confidence_met:{max_conf:.2f}",
            accepted_max_confidence=max_conf,
            auto_rejected_ids=[],
        )

    # Max confidence in [0.8, 0.9) → expansion blocked.
    if consecutive_blocked_loops >= RCA_EXPANSION_BLOCKED_GRACE_LOOPS:
        return ReviewGateResult(
            early_exit=True,
            expansion_blocked=False,
            reason=f"grace_loops_exhausted:{max_conf:.2f}",
            accepted_max_confidence=max_conf,
            auto_rejected_ids=[],
        )

    auto_rejected = _auto_reject_similar(hypotheses, accepted)
    return ReviewGateResult(
        early_exit=False,
        expansion_blocked=True,
        reason=f"expansion_blocked:{max_conf:.2f}",
        accepted_max_confidence=max_conf,
        auto_rejected_ids=auto_rejected,
    )


def _auto_reject_similar(
    hypotheses: list[Hypothesis],
    accepted: list[Hypothesis],
) -> list[str]:
    """Mark PENDING/NEEDS_INVESTIGATION hypotheses that duplicate an accepted
    hypothesis (same category, Jaccard similarity on description tokens)."""
    if not accepted:
        return []

    accepted_tokens: list[tuple[Hypothesis, set[str]]] = [(h, _tokenize(h.description)) for h in accepted]
    rejected_ids: list[str] = []
    for h in hypotheses:
        if h.status not in (
            HypothesisStatus.PENDING,
            HypothesisStatus.NEEDS_INVESTIGATION,
        ):
            continue
        h_tokens = _tokenize(h.description)
        for acc, acc_tokens in accepted_tokens:
            if acc.category != h.category:
                continue
            sim = _jaccard(h_tokens, acc_tokens)
            if sim >= RCA_ACCEPTED_SIMILARITY_THRESHOLD:
                h.status = HypothesisStatus.REJECTED
                rejected_ids.append(h.hypothesis_id)
                logger.info(
                    "Review gate auto-reject %s (sim=%.2f vs %s)",
                    h.hypothesis_id,
                    sim,
                    acc.hypothesis_id,
                )
                break
    return rejected_ids
