"""서버가 기록한 증거로 실행 결과를 확정한다.

해결 판정의 권위는 에이전트의 최종 서술이 아니라 이 모듈에 있다. 모델이 "정상화
되었습니다"라고 말하는 것과 관측이 기준을 만족한 것은 다르며, 후자만 완료의 근거가
된다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from headless_codex.services.execution_contract import (
    authorize_command,
    command_digest,
    step_contract_error,
    validate_steps,
)
from headless_codex.services.execution_evidence import (
    BLOCKED_CLASSES,
    CommandAttempt,
    ExecutionEvidence,
    FailureClass,
    StepEvidence,
    capture_command_output,
    parse_failure_class,
    redact,
    redact_arguments,
)
from headless_codex.services.execution_state import ExecutionState
from headless_codex.services.runbook_contract import validate_recovery_sequence


@dataclass(frozen=True)
class ResolutionVerdict:
    state: ExecutionState
    reason: str


def step_execution_blocker(step: StepEvidence) -> str | None:
    """Reject factual contradictions, not infer command intent or causal recovery.

    A successful exit is necessary evidence of execution, not proof that the
    approved action or its causal success criteria were actually satisfied.
    Failed reads may be retried; a recorded policy block or manual requirement
    cannot be cleared by a model's later positive assertion.
    """
    if step.contract_error:
        return f"step {step.step_id}: {step.contract_error}"
    if any(attempt.blocked is not False or attempt.failure_class in BLOCKED_CLASSES for attempt in step.attempts):
        return f"step {step.step_id} has a blocked attempt and requires manual action"
    if step.manual_action_required is not False:
        return f"step {step.step_id} still requires manual action"
    if not step.is_metric_wait and not any(
        attempt.succeeded is True
        and attempt.blocked is False
        # Legacy server records may omit exit status; an explicit nonzero exit
        # must never be overridden by a contradictory succeeded flag.
        and attempt.exit_status in ("", "0")
        for attempt in step.attempts
    ):
        return f"step {step.step_id} has no successful unblocked command attempt"
    return None


def metric_wait_blocks_success(waits: list[dict]) -> bool:
    """Require a terminal for each selected wait step and keep any terminal failure.

    Receipt order does not change the result. Consume every record so malformed
    legacy identities still raise instead of being hidden by an earlier failure.
    """
    terminal_seen: dict[object, bool] = {}
    failed = False
    for record in waits:
        step_id = record.get("step_id")
        terminal_seen.setdefault(step_id, False)
        # A non-reflexive legacy ID (NaN) matched no records in the previous
        # equality-based scans, so it must remain a wait without a terminal.
        if step_id == step_id and record.get("phase") == "terminal":
            terminal_seen[step_id] = True
            if record.get("status") != "HEALTHY":
                failed = True
    return failed or not all(terminal_seen.values())


def _steps_blocker(evidence: ExecutionEvidence) -> str | None:
    if not evidence.steps:
        return "execution has no approved steps"
    skipped = [step.step_id for step in evidence.steps if not step.attempts and not step.is_metric_wait]
    if skipped:
        return f"steps were not attempted: {', '.join(skipped)}"
    for step in evidence.steps:
        if reason := step_execution_blocker(step):
            return reason
    unmet = [step.step_id for step in evidence.steps if step.resolved is not True and step.resolved is not None]
    if unmet:
        return f"steps did not meet their success criteria: {', '.join(unmet)}"
    unobserved = [step.step_id for step in evidence.steps if step.resolved is None or not step.observation.strip()]
    if unobserved:
        return f"steps have no recorded observation: {', '.join(unobserved)}"
    return None


def _completed_write_proven(terminal: dict, records: list[dict], playbook: dict, execution_id: str) -> bool:
    """Replay producer provenance or actual committed-write logs, never a model success flag.

    Deployment recovery requires a real write in the fixed recovery window. A
    server-bound producer descriptor can prove both bins; otherwise an approved
    read must return a committed write from one of the converged application tasks.
    """
    from headless_codex.services.command_gate import evaluate_command
    from headless_codex.services.post_action_metrics import _completed_accounting, timestamp

    try:
        bound = terminal["binding"]
        request = bound["request"]
        start, end = timestamp(bound["start"]), timestamp(bound["end"])
        if end - start != 120 or start % 60:
            return False
        if terminal.get("write_semantics_verified") is True:
            descriptor = _completed_accounting(
                request,
                records,
                {"playbook": playbook},
                execution_id,
                [s["step_id"] for s in playbook["execution_steps"]],
                evaluate_command,
            )
            if descriptor and descriptor == bound.get("completed_write_accounting"):
                bins = terminal["bins"]
                if len(bins) == 2 and all(
                    timestamp(row["start"]) == start + index * 60
                    and timestamp(row["end"]) == start + (index + 1) * 60
                    and type(row["successful_writes"]) in (int, float)
                    and math.isfinite(row["successful_writes"])
                    and row["successful_writes"] > 0
                    and row["failures"] == 0
                    and row["successful_writes"] == row["attempts"] - row["failures"]
                    for index, row in enumerate(bins)
                ):
                    return True
        convergence = next(
            r
            for r in records
            if r.get("type") == "deployment_wait"
            and r.get("step_id") == request["deployment_step_id"]
            and r.get("phase") == "terminal"
            and r.get("status") == "HEALTHY"
        )
        scope = playbook["rollback_context"]["scope"]
        task_ids = {arn.rsplit("/", 1)[-1] for arn in convergence["task_arns"]}
        for record in records:
            if _committed_log_observed(record, scope, task_ids, request["region"], execution_id, start, end):
                return True
    except (ValueError, TypeError, KeyError, IndexError, StopIteration, OverflowError):
        return False
    return False


def _committed_log_observed(
    record: dict,
    scope: dict,
    task_ids: set[str],
    region: str,
    execution_id: str,
    start: float,
    end: float,
) -> bool:
    """Accept only intact approved log reads tied to a converged task and fixed UTC window."""
    from headless_codex.services.command_gate import evaluate_command
    from headless_codex.services.post_action_metrics import _unaltered_aws_observation, timestamp

    if (
        record.get("type") != "attempt"
        or record.get("execution_id") != execution_id
        or record.get("succeeded") is not True
        or str(record.get("exit_status")) != "0"
        or record.get("blocked")
        or record.get("stdout_truncated")
        or record.get("output_incomplete")
    ):
        return False
    try:
        verdict = evaluate_command(record.get("command", ""))
        if (
            not verdict.allowed
            or not _unaltered_aws_observation(verdict.argv)
            or verdict.service != "logs"
            or verdict.operation
            not in {
                "filter-log-events",
                "get-log-events",
            }
        ):
            return False
        argv = list(verdict.argv)
        options = {}
        for index, token in enumerate(argv):
            if token.startswith("--") and "=" in token:
                key, value = token.split("=", 1)
                options[key] = value
            elif token.startswith("--") and index + 1 < len(argv):
                options[token] = argv[index + 1]
        if options.get("--region") != region or options.get("--log-group-name") != scope["log_group"]:
            return False
        payload = json.loads(record["stdout"])
        for event in payload.get("events", []):
            stream = event.get("logStreamName", options.get("--log-stream-name", "")).split("/")
            if len(stream) < 3 or stream[-2] != scope["container_name"] or stream[-1] not in task_ids:
                continue
            message = json.loads(event["message"])
            observed = timestamp(message["observed_at"])
            logged = event["timestamp"] / 1000
            count = message.get("count")
            if (
                message.get("event") == "write_completed"
                and message.get("completion_semantics") == "committed_rows"
                and type(count) is int
                and count > 0
                and start <= observed < end
                and start <= logged < end
                and abs(observed - logged) <= 60
            ):
                return True
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False
    return False


def _deployment_recovery_error(step: dict, records: list[dict], playbook: dict, execution_id: str) -> str | None:
    """Keep a healthy arithmetic receipt unresolved until server-observed writes prove recovery."""
    if not (step.get("metric_wait") or {}).get("deployment_step_id"):
        return None
    terminal = [
        r
        for r in records
        if r.get("type") == "metric_wait" and r.get("step_id") == step["step_id"] and r.get("phase") == "terminal"
    ]
    if len(terminal) != 1 or terminal[0].get("ok") is not True:
        return "deployment recovery has no successful metric receipt"
    if not _completed_write_proven(terminal[0], records, playbook, execution_id):
        return "deployment recovery requires verified completed-write accounting or actual committed-write evidence"
    return None


def _as_str(value: object, *, limit: int | None = 4000) -> str:
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
    if isinstance(record.get("ecs_control_projection"), dict):
        result["ecs_control_projection"] = json.loads(
            redact(json.dumps(record["ecs_control_projection"], ensure_ascii=False))
        )
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
        playbook_id=_as_str(playbook.get("playbook_id"), limit=None),
        engine=engine,
        started_at=started_at,
        ended_at=ended_at,
    )

    declared_steps = playbook.get("execution_steps")
    contract_error = None
    try:
        approved_steps = validate_steps(playbook)
        validate_recovery_sequence(approved_steps)
        declared_steps = approved_steps
    except (ValueError, TypeError, KeyError) as exc:
        approved_steps = []
        contract_error = str(exc)
    declared_step_ids: set[str] = set()
    if isinstance(declared_steps, list):
        for step in declared_steps:
            if not isinstance(step, dict):
                continue
            # Identifiers select approved contracts; preview truncation can merge distinct steps.
            step_id = _as_str(step.get("step_id"), limit=None)
            if not step_id:
                continue
            declared_step_ids.add(step_id)
            tracked = evidence.step(step_id)
            tracked.intent = _as_str(step.get("intent"))
            # This is the approved contract used for exact comparison, not a UI preview.
            criterion = step.get("success_criteria")
            tracked.success_criteria = criterion if isinstance(criterion, str) else ""
            tracked.is_metric_wait = step.get("metric_wait") is not None or step.get("deployment_wait") is not None
            tracked.contract_error = contract_error or step_contract_error(step, [])

    attempt_counts: dict[str, int] = {}
    criteria_mismatches: set[str] = set()
    replayed: list[dict] = []
    journal: list[dict] = []
    for record in records:
        # Independently replay order/identity when assembling persisted evidence.
        if record.get("type") == "attempt" and not record.get("internal_observation") and approved_steps:
            try:
                step = next(s for s in approved_steps if s["step_id"] == record.get("step_id"))
                index = record["command_index"]
                if type(index) is not int or index < 0:
                    raise ValueError("command index must be a nonnegative integer")
                command = step["commands"][index]
                if record.get("command_digest") != command_digest(command):
                    raise ValueError("command digest differs from the approved snapshot")
                if authorize_command(approved_steps, step["step_id"], command, replayed) != index:
                    raise ValueError("command position differs from the approved order")
            except (ValueError, TypeError, KeyError, IndexError, StopIteration):
                contract_error = "execution evidence violates the approved command identity or order"
        if record.get("type") != "command_started":
            replayed.append(record)
        journal.append(record)
        for step in approved_steps:
            evidence.step(step["step_id"]).contract_error = (
                contract_error
                or step_contract_error(step, journal)
                or _deployment_recovery_error(step, journal, playbook, execution_id)
            )
        record_type = record.get("type")
        if record_type == "approval_rejection":
            evidence.approval_rejections.append(json.loads(redact(json.dumps(record, ensure_ascii=False))))
        elif record_type == "command_started":
            evidence.command_starts.append(json.loads(redact(json.dumps(record, ensure_ascii=False))))
        step_id = _as_str(record.get("step_id"), limit=None)
        if step_id and step_id not in declared_step_ids:
            continue
        if record_type in {"metric_wait", "deployment_wait"} and step_id:
            evidence.metric_wait_records.append(json.loads(redact(json.dumps(record, ensure_ascii=False))))

        if record_type == "attempt" and step_id:
            attempt_counts[step_id] = attempt_counts.get(step_id, 0) + 1
            failure_class = parse_failure_class(record.get("failure_class")) if record.get("failure_class") else None
            blocked = record.get("blocked", False) is not False or failure_class in BLOCKED_CLASSES
            evidence.record_attempt(
                CommandAttempt(
                    step_id=step_id,
                    command=redact(record.get("command")),
                    arguments=redact_arguments(record.get("arguments")),
                    exit_status=_as_str(record.get("exit_status"), limit=64),
                    succeeded=record.get("succeeded") is True,
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
            if record.get("manual_action_required", False) is not False:
                step.manual_action_required = True
            criteria = record.get("success_criteria")
            if not isinstance(criteria, str) or criteria != step.success_criteria:
                criteria_mismatches.add(step_id)
                continue
            step.observation = redact(record.get("observation"))[:4000]
            # Evaluate at this point in the log. A later successful attempt
            # cannot retroactively validate an outcome written before it.
            step.resolved = record.get("criteria_met") is True and step_execution_blocker(step) is None

        elif record_type == "resolution":
            evidence.resolution_records.append(_outcome_record(record))
            evidence.resolution_observation = redact(record.get("observation"))[:4000]
            # 관측으로 확정하지 못한 경우도 사람이 읽을 수 있게 사유를 남긴다.
            unobservable = redact(record.get("unobservable_reason"))[:4000]
            if unobservable:
                evidence.resolution_observation = (
                    f"{evidence.resolution_observation}\n[unobservable] {unobservable}".strip()
                )
            evidence.resolution_confirmed = (
                record.get("resolved") is True and not unobservable and _steps_blocker(evidence) is None
            )

    for step_id in criteria_mismatches:
        step = evidence.step(step_id)
        step.resolved = False
        step.observation = (
            f"{step.observation}\n[invalid outcome] success_criteria did not match the approved playbook".strip()
        )

    return evidence


def judge_resolution(evidence: ExecutionEvidence, *, agent_succeeded: bool) -> ResolutionVerdict:
    """실행 증거로 종료 상태를 확정한다.

    에이전트 실행이 정상 종료하고, 모든 승인 절차에 성공한 비차단 시도와 성공 기준
    관측이 있어야 한다. 차단·수동 조치가 남거나 해소를 관측하지 못하면 해결로 전이하지
    않는다. 명령 성공은 필요조건이며 승인 조치의 인과적 효과까지 증명하지는 않는다.
    """
    if agent_succeeded is not True:
        return ResolutionVerdict(ExecutionState.FAILED, "execution agent did not finish")

    if evidence.resolution_confirmed is None:
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            "execution recorded no resolution observation, so resolution cannot be confirmed",
        )
    if reason := _steps_blocker(evidence):
        return ResolutionVerdict(ExecutionState.UNRESOLVED, reason)
    if evidence.resolution_confirmed is not True:
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            "observation did not confirm that the issue was resolved",
        )
    if evidence.resolution_records:
        latest_resolution = evidence.resolution_records[-1]
        if latest_resolution.get("resolved") is not True or latest_resolution.get("unobservable_reason"):
            return ResolutionVerdict(
                ExecutionState.UNRESOLVED,
                "latest resolution record is unconfirmed or unobservable",
            )

    if metric_wait_blocks_success(evidence.metric_wait_records):
        return ResolutionVerdict(ExecutionState.UNRESOLVED, "fixed metric wait did not confirm healthy bins")

    if not evidence.resolution_observation.strip():
        return ResolutionVerdict(
            ExecutionState.UNRESOLVED,
            "resolved=true requires a nonblank resolution observation",
        )

    return ResolutionVerdict(ExecutionState.RESOLVED, evidence.resolution_observation[:500])


def blocked_failure_classes(evidence: ExecutionEvidence) -> set[FailureClass]:
    return {
        attempt.failure_class
        for step in evidence.steps
        for attempt in step.attempts
        if attempt.failure_class in BLOCKED_CLASSES
    }
