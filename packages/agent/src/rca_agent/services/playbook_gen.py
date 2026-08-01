from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rca_agent.config.settings import (
    LLM_DEFAULT_TIMEOUT_SECONDS,
    PLAYBOOK_UPDATE_THRESHOLD,
)
from rca_agent.ports.dto.models import (
    ExecutionStep,
    Playbook,
    PlaybookMatch,
    RcaReport,
    ScopingResult,
)
from rca_agent.ports.interfaces.playbook_store import PlaybookStorePort
from rca_agent.prompts.playbook import (
    PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE,
    PLAYBOOK_USER_PROMPT_TEMPLATE,
)
from rca_agent.utils.embed_key import build_embed_key
from rca_agent.utils.timeout import call_with_timeout

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)


class ExecutionStepOutput(BaseModel):
    step_id: str = ""
    intent: str = ""
    action: str = ""
    success_criteria: str = ""


class PlaybookOutput(BaseModel):
    failure_type: str
    symptom_pattern: str
    severity_criteria: str = ""
    verification_steps: list[str] = Field(default_factory=list)
    execution_steps: list[ExecutionStepOutput] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    escalation_criteria: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    related_metrics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class PlaybookUpdateOutput(BaseModel):
    needs_update: bool = True
    failure_type: str = ""
    symptom_pattern: str = ""
    severity_criteria: str = ""
    verification_steps: list[str] = Field(default_factory=list)
    execution_steps: list[ExecutionStepOutput] = Field(default_factory=list)
    temporary_mitigation: str = ""
    permanent_remediation: str = ""
    escalation_criteria: str = ""
    prevention_measures: list[str] = Field(default_factory=list)
    related_metrics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def build_execution_steps(
    outputs: list[ExecutionStepOutput],
    *,
    confirmed: bool,
) -> list[ExecutionStep]:
    """실행 절차를 실행 가능한 형태로만 받아들인다.

    미확정 원인에는 실행 절차를 두지 않는다 — 추측 절차가 승인 버튼 뒤에 놓이면
    사람이 검증된 절차로 오인한다. 확정된 경우에도 대상 작업과 관측 기준이 없는 단계는
    실행 근거가 될 수 없으므로 버린다. 실행 에이전트가 성공을 판정할 수 없기 때문이다.
    """
    if not confirmed:
        return []
    steps: list[ExecutionStep] = []
    seen: set[str] = set()
    for index, output in enumerate(outputs, start=1):
        if not output.action.strip() or not output.success_criteria.strip():
            logger.info("Dropping execution step %d — no action or no observable criterion", index)
            continue
        step_id = output.step_id.strip() or f"step-{index}"
        if step_id in seen:
            logger.info("Dropping execution step %s — duplicate step_id", step_id)
            continue
        seen.add(step_id)
        steps.append(
            ExecutionStep(
                step_id=step_id,
                intent=output.intent.strip(),
                action=output.action.strip(),
                success_criteria=output.success_criteria.strip(),
            )
        )
    return steps


def _metric_name(scoping_result: ScopingResult | None) -> str:
    if scoping_result and scoping_result.raw_alarm and scoping_result.raw_alarm.trigger:
        return scoping_result.raw_alarm.trigger.metric_name
    return ""


def _build_embed_key(playbook: Playbook, scoping_result: ScopingResult | None) -> str:
    """Render the search text from the same fields the index stores.

    Sharing the renderer is not enough — the fields have to match too. A
    playbook's failure type and symptom pattern are generalized so the procedure
    can be reused on another resource; a report's root cause and incident summary
    name this incident's resource, revision and timestamps. Query with the report
    and the same fault scores low against its own stored playbook, and the failure
    arrives as an empty result set rather than as an error.
    """
    return build_embed_key(
        failure_type=playbook.failure_type or "unknown",
        symptom=playbook.symptom_pattern,
        metric_name=_metric_name(scoping_result),
    )


def _build_user_prompt(report: RcaReport) -> str:
    return PLAYBOOK_USER_PROMPT_TEMPLATE.format(
        failure_type="Inferred from root cause",
        root_cause=report.root_cause,
        severity=report.severity,
        evidence_highlights="\n".join(f"- {e}" for e in report.evidence_list[:5]) or "N/A",
        detection_method=report.detection_method or "N/A",
        mitigation_text=report.temporary_mitigation or "N/A",
        remediation_text=report.permanent_remediation or "N/A",
        action_items_text="\n".join(f"- {a}" for a in report.action_items) or "N/A",
        confirmed="yes" if report.root_cause_confirmed else "no — leave execution_steps empty",
    )


def _render_existing_execution_steps(steps: list[ExecutionStep]) -> str:
    if not steps:
        return "  (none)"
    return "\n".join(
        f"  - {step.step_id}: intent={step.intent} | action={step.action} | success_criteria={step.success_criteria}"
        for step in steps
    )


def _build_update_prompt(existing: Playbook, report: RcaReport) -> str:
    return PLAYBOOK_UPDATE_USER_PROMPT_TEMPLATE.format(
        existing_failure_type=existing.failure_type or "N/A",
        existing_symptom_pattern=existing.symptom_pattern or "N/A",
        existing_severity_criteria=existing.severity_criteria or "N/A",
        existing_verification_steps="\n".join(f"  - {s}" for s in existing.verification_steps) or "N/A",
        existing_execution_steps=_render_existing_execution_steps(existing.execution_steps),
        existing_temporary_mitigation=existing.temporary_mitigation or "N/A",
        existing_permanent_remediation=existing.permanent_remediation or "N/A",
        existing_escalation_criteria=existing.escalation_criteria or "N/A",
        existing_prevention_measures="\n".join(f"  - {m}" for m in existing.prevention_measures) or "N/A",
        existing_related_metrics="\n".join(f"  - {m}" for m in existing.related_metrics) or "N/A",
        root_cause=report.root_cause,
        severity=report.severity,
        evidence_highlights="\n".join(f"  - {e}" for e in report.evidence_list[:5]) or "N/A",
        detection_method=report.detection_method or "N/A",
        mitigation_text=report.temporary_mitigation or "N/A",
        remediation_text=report.permanent_remediation or "N/A",
        confirmed="yes" if report.root_cause_confirmed else "no — leave execution_steps empty",
    )


def _invoke_agent(agent: Agent, prompt: str) -> PlaybookOutput:
    result = agent(prompt, structured_output_model=PlaybookOutput)
    return result.structured_output


def _invoke_update_agent(agent: Agent, prompt: str) -> PlaybookUpdateOutput:
    result = agent(prompt, structured_output_model=PlaybookUpdateOutput)
    return result.structured_output


def search_existing_playbooks(
    draft: Playbook,
    scoping_result: ScopingResult | None,
    *,
    playbook_store: PlaybookStorePort,
) -> list[PlaybookMatch]:
    """Find playbooks close enough to update instead of creating a duplicate.

    Searches with this run's own draft rather than with the report, so the query
    and the stored entries come from the same fields. Uses a stricter threshold
    than plain retrieval: merging into the wrong playbook is worse than writing a
    new one.
    """
    return playbook_store.search_similar(
        _build_embed_key(draft, scoping_result),
        threshold=PLAYBOOK_UPDATE_THRESHOLD,
    )


def _try_update_existing(
    existing: Playbook,
    report: RcaReport,
    update_agent: Agent,
    *,
    similarity: float = 0.0,
    timeout_seconds: float = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Playbook | None:
    prompt = _build_update_prompt(existing, report)
    logger.info(
        "Checking update for playbook %s (similarity=%.2f)",
        existing.playbook_id,
        similarity,
    )

    output: PlaybookUpdateOutput | None = None
    try:
        output = call_with_timeout(
            lambda: _invoke_update_agent(update_agent, prompt),
            timeout_seconds,
        )
    except Exception:
        logger.warning("Playbook update check failed for %s", existing.playbook_id)

    if output is None or not output.needs_update:
        if output and not output.needs_update:
            logger.info("Playbook %s is up-to-date, no update needed", existing.playbook_id)
        return None

    logger.info("Updating playbook %s with new RCA findings", existing.playbook_id)
    # An empty field means the LLM had nothing to add, not that the step was
    # dropped — keep the recorded value so a merge never loses past procedure.
    #
    # 검증 상태도 그 보존 대상이다. 여기서 빠뜨리면 기본값 DRAFT로 떨어져, 실행으로
    # 입증된 절차가 보강 한 번에 초안으로 되돌아간다. 분석은 이 값을 올릴 수 없지만
    # 이미 획득한 값을 낮출 수도 없다.
    return Playbook(
        playbook_id=existing.playbook_id,
        failure_type=output.failure_type or existing.failure_type,
        symptom_pattern=output.symptom_pattern or existing.symptom_pattern,
        severity_criteria=output.severity_criteria or existing.severity_criteria,
        verification_steps=output.verification_steps or existing.verification_steps,
        execution_steps=(
            build_execution_steps(output.execution_steps, confirmed=report.root_cause_confirmed)
            or existing.execution_steps
        ),
        temporary_mitigation=output.temporary_mitigation or existing.temporary_mitigation,
        permanent_remediation=output.permanent_remediation or existing.permanent_remediation,
        escalation_criteria=output.escalation_criteria or existing.escalation_criteria,
        prevention_measures=output.prevention_measures or existing.prevention_measures,
        related_metrics=output.related_metrics or existing.related_metrics,
        rca_id=report.rca_id,
        tags=output.tags or existing.tags,
        verification_status=existing.verification_status,
    )


def run_playbook_generation(
    report: RcaReport,
    agent: Agent,
    *,
    playbook_store: PlaybookStorePort,
    scoping_result: ScopingResult | None = None,
    timeout_seconds: float = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Playbook:
    deadline = time.monotonic() + max(0, timeout_seconds)

    # 이번 분석의 초안을 먼저 만든다. 검색 쿼리가 인덱스에 저장된 것과 같은 필드에서
    # 나와야 하고, 그 필드는 초안이 생긴 뒤에만 존재한다. 병합이 성립하면 이 초안은
    # 기존 플레이북을 보강하는 입력이 되고, 성립하지 않으면 그대로 신규 플레이북이다.
    draft = _generate_draft(report, agent, deadline)

    existing_hits = search_existing_playbooks(
        draft,
        scoping_result,
        playbook_store=playbook_store,
    )

    merge_candidates = 0
    for hit in existing_hits:
        # Merging without the recorded procedure would overwrite it under the same
        # id, so a hit we cannot load is left alone rather than half-updated.
        existing = playbook_store.load_detail(hit)
        if existing is None:
            logger.info(
                "Skipping playbook %s — detail unavailable, cannot merge safely",
                hit.playbook_id,
            )
            continue
        merge_candidates += 1
        remaining_seconds = max(0.0, deadline - time.monotonic())
        updated = _try_update_existing(
            existing,
            report,
            agent,
            similarity=hit.similarity,
            timeout_seconds=remaining_seconds,
        )
        if updated is not None:
            return updated

    if merge_candidates:
        logger.info("All %d existing playbooks are up-to-date", merge_candidates)

    return draft


def _generate_draft(report: RcaReport, agent: Agent, deadline: float) -> Playbook:
    """이번 RCA 결과로 플레이북 초안을 만든다.

    생성이 실패해도 최소 정보만 담은 플레이북을 돌려준다 — 플레이북 생성 실패가 RCA
    결과 전체의 손실이 되어서는 안 된다.
    """
    playbook_id = str(uuid.uuid4())
    user_prompt = _build_user_prompt(report)

    logger.info("Generating playbook draft from RCA %s", report.rca_id)

    output: PlaybookOutput | None = None
    try:
        remaining_seconds = max(0.0, deadline - time.monotonic())
        output = call_with_timeout(
            lambda: _invoke_agent(agent, user_prompt),
            remaining_seconds,
        )
    except Exception:
        logger.warning("Playbook generation failed")

    if output is None:
        return Playbook(
            playbook_id=playbook_id,
            failure_type="unknown",
            symptom_pattern=report.incident_summary,
            rca_id=report.rca_id,
        )

    logger.info("Playbook generated: %s (type=%s)", playbook_id, output.failure_type)
    return Playbook(
        playbook_id=playbook_id,
        failure_type=output.failure_type,
        symptom_pattern=output.symptom_pattern,
        severity_criteria=output.severity_criteria,
        verification_steps=output.verification_steps,
        execution_steps=build_execution_steps(
            output.execution_steps,
            confirmed=report.root_cause_confirmed,
        ),
        temporary_mitigation=output.temporary_mitigation,
        permanent_remediation=output.permanent_remediation,
        escalation_criteria=output.escalation_criteria,
        prevention_measures=output.prevention_measures,
        related_metrics=output.related_metrics,
        rca_id=report.rca_id,
        tags=output.tags,
    )
