from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import LLM_DEFAULT_TIMEOUT_SECONDS, MAX_BRANCHING_DEPTH
from rca_agent.ports.dto.models import (
    BranchingResult,
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
)
from rca_agent.prompts import BRANCHING_USER_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


class _ChildItem(BaseModel):
    title: str = Field(
        default="",
        description="짧은 한 줄 제목 (~60자). 부모보다 구체적이어야 한다.",
    )
    description: str = Field(
        description="상세 설명. 부모 가설에서 한 단계 구체화된 근거와 검증 방향.",
    )
    category: HypothesisCategory
    confidence_score: float = Field(ge=0.0, le=1.0)
    required_evidence: list[str] = Field(default_factory=list)


MAX_CHILDREN_PER_BRANCH = 3


class BranchingOutput(BaseModel):
    children: list[_ChildItem] = Field(max_length=MAX_CHILDREN_PER_BRANCH)


BranchingOutput.model_rebuild()


def _build_user_prompt(parent: Hypothesis, evidence_text: str, rejected_descriptions: list[str]) -> str:
    rejected_text = "\n".join(f"- {d}" for d in rejected_descriptions) if rejected_descriptions else "None"
    return BRANCHING_USER_PROMPT_TEMPLATE.format(
        parent_description=parent.description,
        parent_category=parent.category,
        parent_confidence=parent.confidence_score,
        evidence_text=evidence_text or "No evidence collected yet.",
        rejected_text=rejected_text,
    )


def _invoke_agent(agent: Agent, prompt: str) -> BranchingOutput:
    result = agent(prompt, structured_output_model=BranchingOutput)
    return result.structured_output


def _is_duplicate(child_desc: str, parent: Hypothesis, rejected: list[str]) -> bool:
    child_lower = child_desc.lower().strip()
    if child_lower == parent.description.lower().strip():
        return True
    return any(child_lower == r.lower().strip() for r in rejected)


def run_branching(
    parent: Hypothesis,
    evidence_text: str,
    rejected_descriptions: list[str],
    agent: Agent,
    *,
    timeout_seconds: int = LLM_DEFAULT_TIMEOUT_SECONDS,
    max_depth: int = MAX_BRANCHING_DEPTH,
) -> BranchingResult:
    if parent.depth >= max_depth:
        logger.warning(
            "Max branching depth (%d) reached for hypothesis %s",
            max_depth,
            parent.hypothesis_id,
        )
        return BranchingResult(tree_id=parent.tree_id, parent_id=parent.hypothesis_id, children=[])

    user_prompt = _build_user_prompt(parent, evidence_text, rejected_descriptions)
    logger.info("Branching hypothesis %s at depth %d", parent.hypothesis_id, parent.depth)

    output: BranchingOutput | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke_agent, agent, user_prompt)
        try:
            output = future.result(timeout=timeout_seconds)
        except (FuturesTimeoutError, Exception):
            logger.warning("Branching failed for hypothesis %s", parent.hypothesis_id)

    if output is None:
        return BranchingResult(tree_id=parent.tree_id, parent_id=parent.hypothesis_id, children=[])

    children = []
    for item in output.children:
        if _is_duplicate(item.description, parent, rejected_descriptions):
            logger.info("Skipping duplicate child hypothesis: %s", item.description[:60])
            continue
        children.append(
            Hypothesis(
                hypothesis_id=str(uuid.uuid4()),
                title=(item.title or item.description.splitlines()[0])[:80],
                description=item.description,
                category=item.category,
                confidence_score=item.confidence_score,
                required_evidence=item.required_evidence,
                status=HypothesisStatus.PENDING,
                tree_id=parent.tree_id,
                parent_id=parent.hypothesis_id,
                depth=parent.depth + 1,
            )
        )

    if len(children) > MAX_CHILDREN_PER_BRANCH:
        logger.warning("Truncating children from %d to %d", len(children), MAX_CHILDREN_PER_BRANCH)
        children = children[:MAX_CHILDREN_PER_BRANCH]
    logger.info("Generated %d child hypotheses for %s", len(children), parent.hypothesis_id)
    return BranchingResult(tree_id=parent.tree_id, parent_id=parent.hypothesis_id, children=children)
