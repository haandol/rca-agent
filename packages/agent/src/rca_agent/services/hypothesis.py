from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import (
    HYPOTHESIS_GENERATION_MAX_RETRIES,
    HYPOTHESIS_GENERATION_TIMEOUT_SECONDS,
)
from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationResult,
    PlaybookMatch,
    ScopingResult,
)
from rca_agent.prompts import HYPOTHESIS_GENERATION_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


MAX_HYPOTHESES_PER_LEVEL = 5


class HypothesisOutput(BaseModel):
    """Structured output model for the hypothesis generation agent."""

    hypotheses: list[_HypothesisItem] = Field(max_length=MAX_HYPOTHESES_PER_LEVEL)


class _HypothesisItem(BaseModel):
    description: str
    category: HypothesisCategory
    confidence_score: float = Field(ge=0.0, le=1.0)
    required_evidence: list[str] = Field(default_factory=list)
    referenced_playbook_id: str | None = None


# Re-declare HypothesisOutput after _HypothesisItem is defined (forward ref)
HypothesisOutput.model_rebuild()


def _build_playbook_context(playbooks: list[PlaybookMatch]) -> str:
    if not playbooks:
        return "No similar playbooks found."
    lines = ["## Similar Playbooks (from past incidents)"]
    for i, pb in enumerate(playbooks, 1):
        lines.append(f"{i}. **{pb.title}** (similarity: {pb.similarity:.2f}, id: {pb.playbook_id})")
        if pb.root_cause_summary:
            lines.append(f"   Root cause: {pb.root_cause_summary}")
    return "\n".join(lines)


def _build_metric_snapshot_text(metric_snapshot: dict) -> str:
    if not metric_snapshot:
        return "No metric data available."
    lines = []
    for name, data in metric_snapshot.items():
        current = data.get("current", "N/A")
        baseline = data.get("baseline", "N/A")
        unit = data.get("unit", "")
        lines.append(f"- **{name}**: current={current}, baseline={baseline} {unit}")
    return "\n".join(lines)


def _build_user_prompt(scoping: ScopingResult) -> str:
    return HYPOTHESIS_GENERATION_USER_PROMPT_TEMPLATE.format(
        alarm_summary=scoping.alarm_summary,
        anomaly_start_time=scoping.anomaly_start_time or "N/A",
        blast_radius=scoping.blast_radius,
        initial_severity=scoping.initial_severity,
        metric_snapshot=_build_metric_snapshot_text(scoping.metric_snapshot),
        playbook_context=_build_playbook_context(scoping.similar_playbooks),
    )


def _invoke_hypothesis_agent(
    agent: Agent,
    user_prompt: str,
) -> HypothesisOutput:
    result = agent(user_prompt, structured_output_model=HypothesisOutput)
    return result.structured_output


def run_hypothesis_generation(
    scoping_result: ScopingResult,
    agent: Agent,
    *,
    timeout_seconds: int = HYPOTHESIS_GENERATION_TIMEOUT_SECONDS,
    max_retries: int = HYPOTHESIS_GENERATION_MAX_RETRIES,
) -> HypothesisGenerationResult:
    """Generate root cause hypotheses from scoping results.

    Retries up to max_retries on parsing failure.
    Enforces timeout_seconds per attempt.
    """
    tree_id = str(uuid.uuid4())
    user_prompt = _build_user_prompt(scoping_result)

    logger.info("Generating hypotheses (tree_id=%s, timeout=%ds)", tree_id, timeout_seconds)

    output: HypothesisOutput | None = None
    last_error: Exception | None = None

    for attempt in range(max_retries):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_invoke_hypothesis_agent, agent, user_prompt)
            try:
                output = future.result(timeout=timeout_seconds)
                break
            except FuturesTimeoutError:
                logger.warning("Hypothesis generation timed out (attempt %d/%d)", attempt + 1, max_retries)
                future.cancel()
                last_error = TimeoutError("Hypothesis generation timed out")
            except Exception as exc:
                logger.warning("Hypothesis generation failed (attempt %d/%d): %s", attempt + 1, max_retries, exc)
                last_error = exc

    if output is None:
        logger.error("Hypothesis generation failed after %d attempts: %s", max_retries, last_error)
        return HypothesisGenerationResult(
            tree_id=tree_id,
            hypotheses=[],
            scoping_result=scoping_result,
        )

    hypotheses = _convert_output_to_hypotheses(output, tree_id)
    if len(hypotheses) > MAX_HYPOTHESES_PER_LEVEL:
        logger.warning("Truncating hypotheses from %d to %d", len(hypotheses), MAX_HYPOTHESES_PER_LEVEL)
        hypotheses = hypotheses[:MAX_HYPOTHESES_PER_LEVEL]
    logger.info("Generated %d hypotheses (tree_id=%s)", len(hypotheses), tree_id)

    return HypothesisGenerationResult(
        tree_id=tree_id,
        hypotheses=hypotheses,
        scoping_result=scoping_result,
    )


def _convert_output_to_hypotheses(output: HypothesisOutput, tree_id: str) -> list[Hypothesis]:
    hypotheses = []
    for item in output.hypotheses:
        hypotheses.append(
            Hypothesis(
                hypothesis_id=str(uuid.uuid4()),
                description=item.description,
                category=item.category,
                confidence_score=item.confidence_score,
                required_evidence=item.required_evidence,
                referenced_playbook_id=item.referenced_playbook_id,
                tree_id=tree_id,
                parent_id=None,
                depth=0,
            )
        )
    return hypotheses
