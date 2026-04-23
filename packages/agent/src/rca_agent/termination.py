from __future__ import annotations

import logging
import time

from rca_agent.config import (
    RCA_MAX_TREE_DEPTH,
    RCA_MAX_VALIDATION_LOOPS,
    RCA_TIME_BUDGET_SECONDS,
    TERMINATION_CONFIDENCE_THRESHOLD,
)
from rca_agent.models import (
    Hypothesis,
    HypothesisStatus,
    TerminationDecision,
    TerminationReason,
    ValidationJudgment,
)

logger = logging.getLogger(__name__)


def check_termination(
    *,
    judgments: list[ValidationJudgment],
    hypotheses: list[Hypothesis],
    start_time: float,
    validation_loop_count: int,
    max_tree_depth: int | None = None,
    time_budget: int = RCA_TIME_BUDGET_SECONDS,
    max_loops: int = RCA_MAX_VALIDATION_LOOPS,
    max_depth: int = RCA_MAX_TREE_DEPTH,
    confidence_threshold: float = TERMINATION_CONFIDENCE_THRESHOLD,
) -> TerminationDecision:
    effective_max_depth = max_tree_depth if max_tree_depth is not None else max_depth

    confirmed = [j for j in judgments if j.status == HypothesisStatus.CONFIRMED]
    if confirmed:
        best = max(confirmed, key=lambda j: j.confidence_score)
        if best.confidence_score >= confidence_threshold:
            hyp = _find_hypothesis(best.hypothesis_id, hypotheses)
            logger.info("Termination: CONFIRMED with confidence %.2f", best.confidence_score)
            return TerminationDecision(
                should_terminate=True,
                reason=TerminationReason.CONFIRMED,
                best_hypothesis=hyp,
            )

    elapsed = time.monotonic() - start_time
    if elapsed >= time_budget:
        logger.warning("Termination: time budget exceeded (%.0fs >= %ds)", elapsed, time_budget)
        return TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.TIME_BUDGET,
            best_hypothesis=_best_hypothesis(judgments, hypotheses),
        )

    current_max_depth = max((h.depth for h in hypotheses), default=0)
    if current_max_depth > effective_max_depth:
        logger.warning("Termination: max tree depth exceeded (%d > %d)", current_max_depth, effective_max_depth)
        return TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.MAX_DEPTH,
            best_hypothesis=_best_hypothesis(judgments, hypotheses),
        )

    if validation_loop_count > max_loops:
        logger.warning("Termination: max validation loops exceeded (%d > %d)", validation_loop_count, max_loops)
        return TerminationDecision(
            should_terminate=True,
            reason=TerminationReason.MAX_LOOPS,
            best_hypothesis=_best_hypothesis(judgments, hypotheses),
        )

    return TerminationDecision(should_terminate=False)


def _find_hypothesis(hypothesis_id: str, hypotheses: list[Hypothesis]) -> Hypothesis | None:
    for h in hypotheses:
        if h.hypothesis_id == hypothesis_id:
            return h
    return None


def _best_hypothesis(judgments: list[ValidationJudgment], hypotheses: list[Hypothesis]) -> Hypothesis | None:
    if not judgments:
        return None
    best_j = max(judgments, key=lambda j: j.confidence_score)
    return _find_hypothesis(best_j.hypothesis_id, hypotheses)
