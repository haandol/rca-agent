from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import (
    CONFIRMATION_THRESHOLD,
    LLM_DEFAULT_TIMEOUT_SECONDS,
    REJECTION_THRESHOLD,
)
from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisStatus,
    ValidationJudgment,
    ValidationResult,
)
from rca_agent.prompts import VALIDATION_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


class _JudgmentItem(BaseModel):
    status: HypothesisStatus
    confidence_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""
    evidence_summary: list[str] = Field(default_factory=list)


class ValidationOutput(BaseModel):
    judgment: _JudgmentItem


ValidationOutput.model_rebuild()


def _classify_status(score: float) -> HypothesisStatus:
    if score >= CONFIRMATION_THRESHOLD:
        return HypothesisStatus.CONFIRMED
    if score <= REJECTION_THRESHOLD:
        return HypothesisStatus.REJECTED
    return HypothesisStatus.NEEDS_INVESTIGATION


def _build_user_prompt(hypothesis: Hypothesis, evidence_text: str) -> str:
    return VALIDATION_USER_PROMPT_TEMPLATE.format(
        description=hypothesis.description,
        category=hypothesis.category,
        previous_confidence=hypothesis.confidence_score,
        evidence_text=evidence_text or "No evidence collected yet.",
    )


def _invoke_agent(agent: Agent, prompt: str) -> ValidationOutput:
    result = agent(prompt, structured_output_model=ValidationOutput)
    return result.structured_output


def validate_hypothesis(
    hypothesis: Hypothesis,
    evidence_text: str,
    agent: Agent,
    *,
    timeout_seconds: int = LLM_DEFAULT_TIMEOUT_SECONDS,
    evidence_failed: bool = False,
) -> ValidationJudgment:
    user_prompt = _build_user_prompt(hypothesis, evidence_text)

    logger.info("Validating hypothesis %s: %s", hypothesis.hypothesis_id, hypothesis.description[:60])

    output: ValidationOutput | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke_agent, agent, user_prompt)
        try:
            output = future.result(timeout=timeout_seconds)
        except (FuturesTimeoutError, Exception):
            logger.warning("Validation failed for hypothesis %s", hypothesis.hypothesis_id)

    if output is None:
        return ValidationJudgment(
            hypothesis_id=hypothesis.hypothesis_id,
            status=HypothesisStatus.NEEDS_INVESTIGATION,
            confidence_score=hypothesis.confidence_score,
            reasoning="Validation timed out or failed — preserving for further investigation.",
        )

    status = _classify_status(output.judgment.confidence_score)

    if evidence_failed and hypothesis.required_evidence and status == HypothesisStatus.CONFIRMED:
        logger.warning(
            "Capping %s from CONFIRMED to NEEDS_INVESTIGATION — evidence failed",
            hypothesis.hypothesis_id,
        )
        status = HypothesisStatus.NEEDS_INVESTIGATION

    logger.info(
        "Validation result for %s: %s (confidence=%.2f)",
        hypothesis.hypothesis_id,
        status,
        output.judgment.confidence_score,
    )

    return ValidationJudgment(
        hypothesis_id=hypothesis.hypothesis_id,
        status=status,
        confidence_score=output.judgment.confidence_score,
        reasoning=output.judgment.reasoning,
        evidence_summary=output.judgment.evidence_summary,
    )


def run_validation(
    hypotheses: list[Hypothesis],
    evidence_map: dict[str, str],
    agent: Agent,
    *,
    timeout_seconds: int = LLM_DEFAULT_TIMEOUT_SECONDS,
    evidence_failed_ids: set[str] | None = None,
) -> ValidationResult:
    tree_id = hypotheses[0].tree_id if hypotheses else ""
    judgments = []
    _failed = evidence_failed_ids or set()

    for h in hypotheses:
        evidence_text = evidence_map.get(h.hypothesis_id, "")
        judgment = validate_hypothesis(
            h,
            evidence_text,
            agent,
            timeout_seconds=timeout_seconds,
            evidence_failed=h.hypothesis_id in _failed,
        )
        judgments.append(judgment)

    all_rejected = all(j.status == HypothesisStatus.REJECTED for j in judgments)
    if all_rejected:
        logger.warning("All hypotheses rejected (tree_id=%s)", tree_id)

    return ValidationResult(tree_id=tree_id, judgments=judgments, all_rejected=all_rejected)
