"""서버가 기록한 증거로 실행 결과를 확정한다.

해결 판정의 권위는 에이전트의 최종 서술이 아니라 이 모듈에 있다. 모델이 "정상화
되었습니다"라고 말하는 것과 관측이 기준을 만족한 것은 다르며, 후자만 완료의 근거가
된다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from headless_codex.services.execution_evidence import (
    BLOCKED_CLASSES,
    CommandAttempt,
    ExecutionEvidence,
    FailureClass,
    capture_command_output,
    parse_failure_class,
    redact,
    redact_arguments,
)
from headless_codex.services.execution_state import ExecutionState


@dataclass(frozen=True)
class ResolutionVerdict:
    state: ExecutionState
    reason: str


def _as_str(value: object, *, limit: int = 4000) -> str:
    if value is None:
        return ""
    return (value if isinstance(value, str) else str(value))[:limit]


def _recorded_time(record: dict, name: str) -> str | None:
    """Carry a timestamp only when the server record supplies it; never infer historical moments."""
    value = record.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _captured_output(record: dict) -> dict:
    """Carry bounded redacted streams and original loss counts; legacy previews stay unidentified.

    Reapplying the cap cannot reset an earlier truncation flag or imply that a timeout captured
    complete output. Records without streams acquire neither streams nor completeness claims.
    """
    captured = capture_command_output(record.get("stdout"), record.get("stderr"))
    result: dict = {}
    for name in ("stdout", "stderr"):
        if name not in record:
            continue
        for key, value in captured.items():
            if key == name or key.startswith(name + "_"):
                result[key] = value
        original_count = record.get(f"{name}_chars")
        if isinstance(original_count, int) and not isinstance(original_count, bool):
            result[f"{name}_chars"] = max(original_count, result[f"{name}_chars"])
            result[f"{name}_omitted_chars"] = result[f"{name}_chars"] - result[f"{name}_retained_chars"]
        result[f"{name}_truncated"] = bool(record.get(f"{name}_truncated")) or result[f"{name}_omitted_chars"] > 0
    for name, limit in (("observation", 2000), ("error_output", 4000)):
        if name not in record:
            continue
        safe = redact(record[name])
        original_count = record.get(f"{name}_chars")
        known_count = (
            max(original_count, len(safe))
            if isinstance(original_count, int) and not isinstance(original_count, bool)
            else len(safe)
        )
        # Legacy observations may already be shortened. Only describe a new loss or carry
        # the server's explicit counts; do not retrospectively claim old previews were complete.
        if len(safe) > limit or f"{name}_chars" in record:
            result.update(
                {
                    f"{name}_chars": known_count,
                    f"{name}_retained_chars": min(len(safe), limit),
                    f"{name}_omitted_chars": known_count - min(len(safe), limit),
                    f"{name}_truncated": bool(record.get(f"{name}_truncated")) or known_count > limit,
                }
            )
    for name in ("output_incomplete", "output_incomplete_reason"):
        if name in record:
            result[name] = redact(record[name]) if isinstance(record[name], str) else record[name]
    return result


def _outcome_record(record: dict) -> dict:
    """Retain outcome fields and server recording time, redacting text without inventing observations."""
    names = (
        "step_id",
        "success_criteria",
        "observation",
        "criteria_met",
        "failure_class",
        "manual_action_required",
        "resolved",
        "unobservable_reason",
    )
    result = {
        name: redact(record[name]) if isinstance(record[name], str) else record[name]
        for name in names
        if name in record
    }
    if (moment := _recorded_time(record, "recorded_at")) is not None:
        result["recorded_at"] = moment
    return result


def assemble_evidence(
    records: list[dict],
    *,
    execution_id: str,
    rca_id: str,
    engine: str,
    playbook: dict,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> ExecutionEvidence:
    """서버 기록을 플레이북 절차에 맞춰 실행 증거로 조립한다.

    절차 목록은 플레이북이 보유하므로, 에이전트가 언급하지 않은 절차도 증거에
    나타난다. 시도되지 않은 절차가 조용히 사라지면 실행이 절차를 건너뛴 사실을
    사람이 알 수 없다. 시각은 서버 원본 또는 호출자가 실제 실행 경계에서 포착한
    시각만 전달하며 조립 시각을 과거 명령의 시각으로 사용하지 않는다.
    """
    evidence = ExecutionEvidence(
        execution_id=execution_id,
        rca_id=rca_id,
        playbook_id=_as_str(playbook.get("playbook_id"), limit=200),
        engine=engine,
        started_at=started_at,
        ended_at=ended_at,
    )

    declared_steps = playbook.get("execution_steps")
    declared_step_ids: set[str] = set()
    if isinstance(declared_steps, list):
        for step in declared_steps:
            if not isinstance(step, dict):
                continue
            step_id = _as_str(step.get("step_id"), limit=200)
            if not step_id:
                continue
            declared_step_ids.add(step_id)
            tracked = evidence.step(step_id)
            tracked.intent = _as_str(step.get("intent"))
            tracked.success_criteria = _as_str(step.get("success_criteria"))

    attempt_counts: dict[str, int] = {}
    criteria_mismatches: set[str] = set()
    for record in records:
        record_type = record.get("type")
        step_id = _as_str(record.get("step_id"), limit=200)
        if step_id and step_id not in declared_step_ids:
            continue
        if record_type == "metric_wait" and step_id:
            evidence.metric_wait_records.append(json.loads(redact(json.dumps(record, ensure_ascii=False))))

        if record_type == "attempt" and step_id:
            attempt_counts[step_id] = attempt_counts.get(step_id, 0) + 1
            failure_class = parse_failure_class(record.get("failure_class")) if record.get("failure_class") else None
            blocked = bool(record.get("blocked"))
            evidence.record_attempt(
                CommandAttempt(
                    step_id=step_id,
                    command=redact(record.get("command")),
                    arguments=redact_arguments(record.get("arguments")),
                    exit_status=_as_str(record.get("exit_status"), limit=64),
                    succeeded=bool(record.get("succeeded")),
                    attempt_index=attempt_counts[step_id],
                    error_output=redact(record.get("error_output"))[:4000],
                    failure_class=failure_class,
                    blocked=blocked,
                    block_reason=redact(record.get("block_reason"))[:4000],
                    observation=redact(record.get("observation"))[:2000],
                    intent=redact(record.get("intent")),
                    recorded_at=_recorded_time(record, "recorded_at"),
                    started_at=_recorded_time(record, "started_at"),
                    ended_at=_recorded_time(record, "ended_at"),
                    captured_output=_captured_output(record),
                )
            )
            if blocked:
                evidence.step(step_id).manual_action_required = True

        elif record_type == "step_outcome" and step_id:
            step = evidence.step(step_id)
            step.outcomes.append(_outcome_record(record))
            criteria = record.get("success_criteria")
            if not isinstance(criteria, str) or criteria != step.success_criteria:
                criteria_mismatches.add(step_id)
                continue
            step.observation = redact(record.get("observation"))[:4000]
            step.resolved = bool(record.get("criteria_met"))
            if record.get("manual_action_required"):
                step.manual_action_required = True

        elif record_type == "resolution":
            evidence.resolution_records.append(_outcome_record(record))
            evidence.resolution_observation = redact(record.get("observation"))[:4000]
            # 관측으로 확정하지 못한 경우도 사람이 읽을 수 있게 사유를 남긴다.
            unobservable = redact(record.get("unobservable_reason"))[:4000]
            if unobservable:
                evidence.resolution_observation = (
                    f"{evidence.resolution_observation}\n[unobservable] {unobservable}".strip()
                )
            evidence.resolution_confirmed = bool(record.get("resolved"))

    for step_id in criteria_mismatches:
        step = evidence.step(step_id)
        step.resolved = False
        step.observation = (
            f"{step.observation}\n[invalid outcome] success_criteria did not match the approved playbook".strip()
        )

    return evidence


def judge_resolution(evidence: ExecutionEvidence, *, agent_succeeded: bool) -> ResolutionVerdict:
    """실행 증거로 종료 상태를 확정한다.

    해결로 전이하는 조건은 세 가지가 모두 성립할 때다. 에이전트 실행이 정상 종료했고,
    해소가 관측으로 확인되었고, 시도된 절차 중 관측 기준을 만족하지 못한 것이 없어야
    한다. 하나라도 아니면 완료로 전이하지 않는다.
    """
    if not agent_succeeded:
        return ResolutionVerdict(ExecutionState.FAILED, "execution agent did not finish")

    if evidence.resolution_confirmed is None:
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            "execution recorded no resolution observation, so resolution cannot be confirmed",
        )
    if not evidence.resolution_confirmed:
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            "observation did not confirm that the issue was resolved",
        )

    waits = evidence.metric_wait_records
    for step_id in {r.get("step_id") for r in waits}:
        terminal = [r for r in waits if r.get("step_id") == step_id and r.get("phase") == "terminal"]
        if not terminal or any(r.get("status") != "HEALTHY" for r in terminal):
            return ResolutionVerdict(ExecutionState.UNRESOLVED, "fixed metric wait did not confirm healthy bins")

    if not evidence.resolution_observation.strip():
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            "resolved=true requires a nonblank resolution observation",
        )

    skipped = [step.step_id for step in evidence.steps if not step.attempts]
    if skipped:
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            f"steps were not attempted: {', '.join(skipped)}",
        )

    unmet = [step.step_id for step in evidence.steps if step.resolved is False]
    if unmet:
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            f"steps did not meet their success criteria: {', '.join(unmet)}",
        )

    unobserved = [step.step_id for step in evidence.steps if step.resolved is None or not step.observation.strip()]
    if unobserved:
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            f"steps have no recorded observation: {', '.join(unobserved)}",
        )

    return ResolutionVerdict(ExecutionState.RESOLVED, evidence.resolution_observation[:500])


def blocked_failure_classes(evidence: ExecutionEvidence) -> set[FailureClass]:
    return {
        attempt.failure_class
        for step in evidence.steps
        for attempt in step.attempts
        if attempt.failure_class in BLOCKED_CLASSES
    }
