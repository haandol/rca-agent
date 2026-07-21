from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import (
    CONFIRMATION_THRESHOLD,
    LLM_DEFAULT_TIMEOUT_SECONDS,
    REJECTION_THRESHOLD,
)
from rca_agent.ports.dto.models import (
    FaultType,
    Hypothesis,
    HypothesisStatus,
    ValidationJudgment,
    ValidationResult,
)
from rca_agent.prompts.validation import VALIDATION_USER_PROMPT_TEMPLATE
from rca_agent.utils.timeout import call_with_timeout

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


class _JudgmentItem(BaseModel):
    status: HypothesisStatus
    confidence_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""
    evidence_summary: list[str] = Field(default_factory=list)
    validated_fault_type: FaultType = FaultType.UNSUPPORTED


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

    try:
        output = call_with_timeout(
            lambda: _invoke_agent(agent, user_prompt),
            timeout_seconds,
        )
    except Exception:
        logger.warning("Validation failed for hypothesis %s", hypothesis.hypothesis_id)
        output = None

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

    evidence_summary = [summary for summary in output.judgment.evidence_summary if summary.strip()]
    validated_fault_type = FaultType.UNSUPPORTED
    if (
        status == HypothesisStatus.CONFIRMED
        and not evidence_failed
        and evidence_text.strip()
        and evidence_summary
        and output.judgment.validated_fault_type != FaultType.UNSUPPORTED
    ):
        validated_fault_type = output.judgment.validated_fault_type

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
        evidence_summary=evidence_summary,
        validated_fault_type=validated_fault_type,
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
