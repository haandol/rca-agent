from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import LLM_DEFAULT_TIMEOUT_SECONDS
from rca_agent.ports.dto.models import (
    Hypothesis,
    HypothesisCategory,
    PrioritizationResult,
    PrioritizedHypothesis,
    ScopingResult,
    ValidationPlan,
)
from rca_agent.prompts.prioritization import PRIORITIZATION_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

_CATEGORY_FALLBACK_ORDER = {
    HypothesisCategory.DEPLOYMENT: 0,
    HypothesisCategory.INFRASTRUCTURE: 1,
    HypothesisCategory.TRAFFIC: 2,
    HypothesisCategory.DEPENDENCY: 3,
    HypothesisCategory.CONFIGURATION: 4,
}


class _PrioritizedItem(BaseModel):
    hypothesis_id: str
    priority_rank: int
    tools: list[str] = Field(default_factory=list)
    estimated_seconds: int = 60
    parallel_group: int = 0


class PrioritizationOutput(BaseModel):
    prioritized: list[_PrioritizedItem]


PrioritizationOutput.model_rebuild()


def _build_hypotheses_text(hypotheses: list[Hypothesis]) -> str:
    lines = []
    for h in hypotheses:
        lines.append(f"- [{h.hypothesis_id}] ({h.category}) {h.description} (confidence={h.confidence_score:.2f})")
    return "\n".join(lines)


def _build_user_prompt(scoping: ScopingResult, hypotheses: list[Hypothesis]) -> str:
    return PRIORITIZATION_USER_PROMPT_TEMPLATE.format(
        scoping_summary=scoping.alarm_summary,
        hypotheses_text=_build_hypotheses_text(hypotheses),
    )


def _apply_fallback_order(hypotheses: list[Hypothesis]) -> list[PrioritizedHypothesis]:
    sorted_hyps = sorted(hypotheses, key=lambda h: _CATEGORY_FALLBACK_ORDER.get(h.category, 99))
    return [
        PrioritizedHypothesis(
            hypothesis_id=h.hypothesis_id,
            priority_rank=i + 1,
        )
        for i, h in enumerate(sorted_hyps)
    ]


def _invoke_agent(agent: Agent, prompt: str) -> PrioritizationOutput:
    result = agent(prompt, structured_output_model=PrioritizationOutput)
    return result.structured_output


def run_prioritization(
    scoping_result: ScopingResult,
    hypotheses: list[Hypothesis],
    agent: Agent,
    *,
    timeout_seconds: int = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> PrioritizationResult:
    tree_id = hypotheses[0].tree_id if hypotheses else ""
    user_prompt = _build_user_prompt(scoping_result, hypotheses)

    logger.info("Prioritizing %d hypotheses (tree_id=%s)", len(hypotheses), tree_id)

    output: PrioritizationOutput | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke_agent, agent, user_prompt)
        try:
            output = future.result(timeout=timeout_seconds)
        except (FuturesTimeoutError, Exception):
            logger.warning("Prioritization failed, applying category fallback order")

    if output is None:
        return PrioritizationResult(
            tree_id=tree_id,
            prioritized=_apply_fallback_order(hypotheses),
        )

    prioritized = []
    for item in output.prioritized:
        prioritized.append(
            PrioritizedHypothesis(
                hypothesis_id=item.hypothesis_id,
                priority_rank=item.priority_rank,
                validation_plan=ValidationPlan(
                    tools=item.tools,
                    estimated_seconds=item.estimated_seconds,
                ),
                parallel_group=item.parallel_group,
            )
        )

    logger.info("Prioritization complete: %d items", len(prioritized))
    return PrioritizationResult(tree_id=tree_id, prioritized=prioritized)
