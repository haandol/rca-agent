"""플레이북 실행 MCP server.

실행 에이전트에만 제공되는 쓰기 경로다. 분석 실행은 이 서버를 갖지 않는다.

두 가지를 서버가 보유한다. 첫째, **파괴성 판정** — 에이전트가 어떤 명령을 요청하든
서버가 작업 이름을 추출해 거부 어휘와 대조한다. 둘째, **증거 기록** — 시도와 결과가
에이전트의 서술이 아니라 서버가 관측한 사실로 남는다.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import FastMCP
from pydantic import StrictBool, StrictInt

from headless_codex.services.command_gate import evaluate_command
from headless_codex.services.execution_contract import (
    approved_wait,
    authorize_command,
    command_digest,
    command_identity,
    order_error,
    same_request,
    validate_steps,
)
from headless_codex.services.execution_evidence import (
    ExecutionEvidence,
    FailureClass,
    capture_command_output,
    capture_ecs_control_output,
    parse_failure_class,
    redact,
    redact_arguments,
)
from headless_codex.services.execution_outcome import (
    assemble_evidence,
    judge_resolution,
    metric_wait_blocks_success,
    step_execution_blocker,
)
from headless_codex.services.execution_state import ExecutionState
from headless_codex.services.execution_workspace import (
    EXECUTION_ID_ENV,
    EXECUTION_TOKEN_ENV,
    evidence_path_for_token,
    observation_context_path_for_token,
)
from headless_codex.services.post_action_metrics import (
    ObservationBudget,
    ObservationStoppedError,
    bind_request,
    normalize_request,
    poll_fixed_metrics,
    timestamp,
    utc,
)

mcp = FastMCP("playbook-execution")

_COMMAND_TIMEOUT_SECONDS = int(os.environ.get("EXECUTION_COMMAND_TIMEOUT_SECONDS", "300"))


def _now_iso() -> str:
    """Read the server UTC clock at an execution or persistence boundary."""
    return datetime.now(UTC).isoformat()


def _evidence_file() -> Path | None:
    token = os.environ.get(EXECUTION_TOKEN_ENV, "")
    try:
        path = evidence_path_for_token(token)
    except ValueError:
        return None
    return path if path.parent.is_dir() and not path.parent.is_symlink() else None


def _append_record(record: dict) -> bool:
    """Append server evidence with its actual recording time, never a later assembly time."""
    path = _evidence_file()
    if path is None:
        return False
    record = {**record, "recorded_at": _now_iso()}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _read_records() -> list[dict] | None:
    """서버가 기록한 현재 실행의 증거를 읽는다. 깨진 줄은 증거로 인정하지 않는다."""
    path = _evidence_file()
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    records: list[dict] = []
    for line in raw.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _approved_step_ids() -> tuple[str, ...]:
    """Derive identities from the same snapshot that authorizes concrete commands."""
    try:
        return tuple(step["step_id"] for step in _approved_playbook()["execution_steps"])
    except (OSError, ValueError, TypeError, KeyError):
        return ()


def _approved_playbook() -> dict:
    """Read the worker's digest-verified snapshot; never reconstruct authority from model input."""
    context = json.loads(observation_context_path_for_token(os.environ.get(EXECUTION_TOKEN_ENV, "")).read_text())
    playbook = context["playbook"]
    return {**playbook, "execution_steps": validate_steps(playbook)}


def _reject_approval(error: str, step_id: str, command: str = "") -> str:
    """Preserve a scope/order refusal so a later positive narrative cannot erase it."""
    _append_record(
        {"type": "approval_rejection", "step_id": step_id, "command": redact(command), "error": redact(error)}
    )
    return json.dumps({"ok": False, "blocked": True, "error": error, "reason": error}, ensure_ascii=False)


def _approved_success_criteria() -> dict[str, str]:
    """Use the immutable snapshot criterion, not an independently supplied model value."""
    try:
        return {step["step_id"]: step["success_criteria"] for step in _approved_playbook()["execution_steps"]}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def _validate_step_id(step_id: str) -> str | None:
    if not isinstance(step_id, str) or not step_id.strip():
        return "step_id is required"
    if step_id.strip() not in _approved_step_ids():
        return "step_id is not declared in the approved playbook"
    return None


def _classify_exit(stderr: str, returncode: int) -> FailureClass:
    """종료 출력에서 실패 분류를 추정한다.

    분류의 목적은 회고가 절차 결함과 일시적 오류를 구분하는 것이다. 확정할 수 없으면
    UNKNOWN 으로 두어 회고가 교정 대상으로 삼지 않게 한다 — 잘못된 교정보다 미교정이
    안전하다.
    """
    if returncode == 0:
        return FailureClass.UNKNOWN
    lowered = stderr.lower()
    if "throttl" in lowered or "ratelimit" in lowered or "toomanyrequests" in lowered:
        return FailureClass.THROTTLED
    if "timed out" in lowered or "timeout" in lowered:
        return FailureClass.TIMEOUT
    if "accessdenied" in lowered or "not authorized" in lowered or "unauthorizedoperation" in lowered:
        return FailureClass.PERMISSION_DENIED
    if "notfound" in lowered or "does not exist" in lowered or "no such" in lowered:
        return FailureClass.TARGET_NOT_FOUND
    if (
        "validationerror" in lowered
        or "invalidparameter" in lowered
        or "unknown options" in lowered
        or "invalid choice" in lowered
        or "argument" in lowered
    ):
        return FailureClass.INVALID_ARGUMENT
    if "invalidstate" in lowered or "not in a valid state" in lowered or "precondition" in lowered:
        return FailureClass.MISSING_PRECONDITION
    return FailureClass.UNKNOWN


def _run_command(
    step_id: str,
    command: str,
    intent: str = "",
    *,
    budget: ObservationBudget | None = None,
    command_index: int | None = None,
    ecs_observation: bool = False,
    deployment_guard: tuple[dict, dict] | None = None,
) -> str:
    """플레이북 절차의 한 명령을 실행한다. 파괴적·판정 불가 명령은 거부된다.

    Args:
        step_id: 수행 중인 플레이북 실행 절차의 식별자.
        command: 실행할 AWS CLI 명령 한 개. 셸 합성(파이프, 리다이렉션, `&&`,
                 명령 치환)은 판정 불가로 거부된다.
        intent: 이 명령이 절차의 무엇을 달성하려는지.
    """
    step_error = _validate_step_id(step_id)
    if step_error:
        return json.dumps({"ok": False, "error": step_error}, ensure_ascii=False)

    verdict = evaluate_command(command)
    safe_command = redact(command)
    attempt_metadata = {
        "type": "attempt",
        "execution_id": os.environ.get(EXECUTION_ID_ENV, ""),
        "step_id": step_id.strip(),
        "intent": redact(intent),
        "command": safe_command,
        "command_digest": command_digest(command),
        "command_identity": command_identity(command),
        "command_index": command_index,
        "internal_observation": budget is not None,
        "arguments": redact_arguments({"service": verdict.service, "operation": verdict.operation}),
    }

    if not verdict.allowed:
        failure_class = FailureClass.BLOCKED_UNDECIDABLE if verdict.undecidable else FailureClass.BLOCKED_DESTRUCTIVE
        _append_record(
            {
                **attempt_metadata,
                "blocked": True,
                "block_reason": redact(verdict.reason),
                "failure_class": str(failure_class),
                "succeeded": False,
                "exit_status": "blocked",
            }
        )
        # 차단은 실행 전체를 중단시키지 않는다. 해당 절차만 수동 조치로 남는다.
        return json.dumps(
            {
                "ok": False,
                "blocked": True,
                "reason": redact(verdict.reason),
                "guidance": (
                    "This step stays a manual action. Do not retry it or work around the refusal. "
                    "Continue with the remaining steps."
                ),
            },
            ensure_ascii=False,
        )

    if budget is None:
        try:
            _observation_control()
        except ObservationStoppedError as exc:
            return _reject_approval(str(exc), step_id, command)
    started_at = _now_iso()
    if budget is None and not _append_record({**attempt_metadata, "type": "command_started", "started_at": started_at}):
        return json.dumps({"ok": False, "error": "cannot persist command start before execution"})
    command_timeout = _COMMAND_TIMEOUT_SECONDS
    guard_receipt = None
    try:
        if deployment_guard is not None:
            from headless_codex.services.service_deployment import check_precondition

            guard, wait = deployment_guard
            guard_budget = ObservationBudget(
                min(_observation_control(), time.time() + min(_COMMAND_TIMEOUT_SECONDS, 300)), _observation_control
            )
            guard_receipt = check_precondition(
                guard,
                wait,
                guard_budget,
                lambda c, b: json.loads(
                    _run_command(step_id, c, "Server rollback precondition", budget=b, ecs_observation=True)
                ),
            )
            if not _append_record(
                {
                    "type": "ecs_service_precondition",
                    "step_id": step_id,
                    "execution_id": os.environ.get(EXECUTION_ID_ENV, ""),
                    "observed_at": _now_iso(),
                    "receipt": guard_receipt,
                }
            ):
                raise ObservationStoppedError("cannot persist precondition before write")
            _observation_control()
            guard_budget.remaining()
            completed = _run_observation_process(verdict.argv, guard_budget)
        elif budget is None:
            command_timeout = min(_COMMAND_TIMEOUT_SECONDS, _observation_control() - time.time())
            if command_timeout <= 0:
                raise ObservationStoppedError("execution deadline exhausted before command start")
            completed = subprocess.run(  # noqa: S603 - argv comes from the gate, never a shell string
                list(verdict.argv),
                capture_output=True,
                text=True,
                timeout=command_timeout,
                check=False,
            )
        else:
            cw_read = verdict.service == "cloudwatch" and verdict.operation in (
                "get-metric-statistics",
                "describe-alarms",
            )
            ecs_read = (
                ecs_observation
                and verdict.service == "ecs"
                and verdict.operation
                in ("describe-services", "list-tasks", "describe-tasks", "describe-task-definition")
            )
            if not (cw_read or ecs_read):
                raise ObservationStoppedError("bounded observations permit only server-bound reads")
            completed = _run_observation_process(verdict.argv, budget)
    except subprocess.TimeoutExpired as exc:
        ended_at = _now_iso()
        captured = capture_command_output(exc.stdout, exc.stderr)
        _append_record(
            {
                **attempt_metadata,
                **captured,
                "started_at": started_at,
                "ended_at": ended_at,
                "output_incomplete": True,
                "output_incomplete_reason": "timeout",
                "succeeded": False,
                "exit_status": "timeout",
                "failure_class": str(FailureClass.TIMEOUT),
                "error_output": f"command exceeded {command_timeout}s",
            }
        )
        return json.dumps(
            {"ok": False, "error": f"command timed out after {command_timeout}s"},
            ensure_ascii=False,
        )
    except OSError as exc:
        ended_at = _now_iso()
        _append_record(
            {
                **attempt_metadata,
                "started_at": started_at,
                "ended_at": ended_at,
                "succeeded": False,
                "exit_status": "spawn_failed",
                "failure_class": str(FailureClass.UNKNOWN),
                "error_output": redact(str(exc)),
            }
        )
        return json.dumps({"ok": False, "error": f"command could not start: {redact(exc)}"}, ensure_ascii=False)
    except (ObservationStoppedError, ValueError, KeyError, TypeError) as exc:
        stopped = isinstance(exc, ObservationStoppedError)
        captured = capture_command_output(getattr(exc, "stdout", ""), getattr(exc, "stderr", ""))
        _append_record(
            {
                **attempt_metadata,
                **captured,
                "started_at": started_at,
                "ended_at": _now_iso(),
                "succeeded": False,
                "exit_status": "observation_stopped" if stopped else "precondition_failed",
                "failure_class": str(FailureClass.TIMEOUT if stopped else FailureClass.MISSING_PRECONDITION),
                "retry_forbidden": deployment_guard is not None,
                "error_output": redact(str(exc)),
                "output_incomplete": True,
                "output_incomplete_reason": "cancelled, fenced or budget exhausted"
                if stopped
                else "precondition failed",
            }
        )
        return json.dumps({"ok": False, **captured, "output_incomplete": True, "error": str(exc)})

    ended_at = _now_iso()
    captured = (
        capture_ecs_control_output(completed.stdout, completed.stderr)
        if ecs_observation or deployment_guard is not None
        else capture_command_output(completed.stdout, completed.stderr)
    )
    stdout = captured["stdout"]
    stderr = captured["stderr"]
    succeeded = completed.returncode == 0
    failure_class = _classify_exit(stderr, completed.returncode)
    deployment_id = None
    if deployment_guard is not None and succeeded:
        from headless_codex.services.service_deployment import verify_update_response

        try:
            guard_budget.remaining()
            if captured.get("stdout_truncated") or captured.get("output_incomplete"):
                raise ValueError("incomplete UpdateService response")
            deployment_id = verify_update_response(json.loads(stdout), *deployment_guard, guard_receipt)
        except (ValueError, KeyError, TypeError, ObservationStoppedError) as exc:
            succeeded = False
            stderr = str(exc)
            failure_class = FailureClass.UNKNOWN

    _append_record(
        {
            **attempt_metadata,
            **captured,
            "started_at": started_at,
            "ended_at": ended_at,
            "succeeded": succeeded,
            **(
                {
                    "deployment_id": deployment_id,
                    "ecs_service_precondition_receipt": guard_receipt,
                    "retry_forbidden": not succeeded,
                }
                if deployment_guard
                else {}
            ),
            "exit_status": str(completed.returncode),
            "failure_class": str(failure_class) if not succeeded else None,
            "error_output": stderr if not succeeded else "",
            "observation": stdout[:2000],
            "observation_truncated": len(stdout) > 2000 or captured["stdout_truncated"],
            "observation_chars": captured["stdout_chars"],
            "observation_retained_chars": len(stdout[:2000]),
            "observation_omitted_chars": captured["stdout_chars"] - len(stdout[:2000]),
        }
    )

    return json.dumps(
        {
            "ok": succeeded,
            "exit_status": completed.returncode,
            **captured,
            "failure_class": str(failure_class) if not succeeded else None,
        },
        ensure_ascii=False,
    )


def _run_observation_process(argv: tuple[str, ...], budget: ObservationBudget) -> subprocess.CompletedProcess:
    """Use the same gate/attempt audit with a cancellable, remaining-budget-bounded child."""
    remaining = min(_COMMAND_TIMEOUT_SECONDS, budget.remaining())
    if remaining <= 1:
        raise ObservationStoppedError("insufficient remaining command budget")
    # Reserve bounded process cleanup inside the existing call/execution budget.
    command_deadline = budget.monotonic() + remaining - 1
    proc = subprocess.Popen(  # noqa: S603 - only gate-approved fixed CloudWatch reads
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        while True:
            remaining = min(budget.remaining(), command_deadline - budget.monotonic())
            if remaining <= 0:
                raise ObservationStoppedError("command deadline exhausted")
            try:
                stdout, stderr = proc.communicate(timeout=min(0.5, remaining))
                budget.remaining()
                return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    except BaseException as exc:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired as cleanup_error:
            stdout, stderr = cleanup_error.stdout, cleanup_error.stderr
        if isinstance(exc, ObservationStoppedError):
            exc.stdout = stdout
            exc.stderr = stderr
        raise


@mcp.tool()
def run_playbook_command(step_id: str, command: str, intent: str = "") -> str:
    """Serialize exact approved commands and persist write starts before spawning a child."""
    try:
        steps = validate_steps(_approved_playbook())
        if step_id not in [step["step_id"] for step in steps]:
            raise ValueError("step_id is not declared in the approved playbook")
        path = _evidence_file()
        if path is None:
            raise ValueError("missing execution context")
        with (path.parent / "execution-operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return json.dumps({"ok": False, "error": "an execution operation is already running"})
            records = _read_records()
            if records is None:
                raise ValueError("missing execution evidence")
            index = authorize_command(steps, step_id, command, records)
            step = next(s for s in steps if s["step_id"] == step_id)
            guard_pair = None
            if guard := step.get("ecs_service_precondition"):
                wait = next(
                    s["deployment_wait"]
                    for s in steps
                    if (s.get("deployment_wait") or {}).get("action_step_id") == step_id
                )
                guard_pair = (guard, wait)
            return _run_command(step_id, command, intent, command_index=index, deployment_guard=guard_pair)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _reject_approval(str(exc), step_id, command)


def _observation_control() -> float:
    """Read the runner's fail-closed claim/cancellation heartbeat without adding AWS clients."""
    from headless_codex.services.execution_workspace import observation_control_path_for_token

    try:
        path = observation_control_path_for_token(os.environ.get(EXECUTION_TOKEN_ENV, ""))
        control = json.loads(path.read_text())
        now = time.time()
        checked = float(control["checked_at_epoch"])
        deadline = float(control["deadline_epoch"])
        if (
            control.get("execution_id") != os.environ.get(EXECUTION_ID_ENV)
            or control.get("active") is not True
            or not 0 <= now - checked <= 10
            or deadline <= now
        ):
            raise ValueError("inactive, expired or stale execution control")
        if not all(math.isfinite(v) for v in (checked, deadline)):
            raise ValueError("invalid execution control time")
        return deadline
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ObservationStoppedError(f"execution cancellation/claim control unavailable: {exc}") from exc


@mcp.tool()
def wait_for_post_action_metrics(
    step_id: str,
    action_step_id: str = "",
    metrics: dict | None = None,
    failure_alarm_name: str = "",
    region: str = "",
    max_wait_seconds: StrictInt = 900,
    latency_alarm_name: str = "",
    completed_work_evidence: dict | None = None,
    deployment_step_id: str = "",
) -> str:
    """Wait for two fixed 60s bins after StopTask or service convergence; never resolve the execution.

    Pass approved verification/prior action IDs and observed metrics: attempts, failures,
    optional approved latency, each with namespace, metric_name, dimensions (Name-to-Value mapping).
    Discovery must already be a preceding approved commands step through run_playbook_command.
    Use action_step_id for StopTask or deployment_step_id for the approved service convergence step.
    Exactly one reference is required. The server binds the same-execution timestamp, thresholds and scope.
    Repeating this request replays its terminal receipt; changing it cannot rebase bins.
    Optional completed_work_evidence references an actual observed producer accounting descriptor
    with record_index and json_pointer; otherwise arithmetic is not labeled successful writes.
    """
    from headless_codex.services.execution_workspace import observation_context_path_for_token

    request = None
    approved_request = None
    try:
        steps = validate_steps(_approved_playbook())
        for candidate in (step_id, deployment_step_id or action_step_id):
            if error := _validate_step_id(candidate):
                raise ValueError(error)
        request = normalize_request(
            step_id,
            action_step_id,
            metrics,
            failure_alarm_name,
            latency_alarm_name,
            region,
            max_wait_seconds,
            completed_work_evidence,
            deployment_step_id,
        )
        step = next((s for s in steps if s["step_id"] == step_id), {})
        approved_request = approved_wait(step) if "metric_wait" in step else None
        if approved_request is None or not same_request(approved_request, request):
            return _reject_approval("metric_wait arguments do not exactly match the approved snapshot", step_id)
        path = _evidence_file()
        if path is None:
            raise ValueError("missing execution context")
        # Nonblocking process lock also excludes a second MCP process in the same workspace.
        with (path.parent / "execution-operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return json.dumps({"ok": False, "error": "an observation wait is already running"})
            records = _read_records()
            if records is None:
                raise ValueError("missing execution evidence")
            previous = [r for r in records if r.get("type") == "metric_wait" and r.get("step_id") == step_id]
            started = next((r for r in previous if r.get("phase") == "started"), None)
            terminal = next((r for r in previous if r.get("phase") == "terminal"), None)
            if terminal:
                if not same_request(terminal.get("binding", {}).get("request", {}), request):
                    return _reject_approval("terminal wait request differs from approved arguments", step_id)
                return json.dumps(terminal, ensure_ascii=False)
            if started:
                if started["binding"]["request"] != request:
                    raise ValueError("verification request is immutable; different anchor or arguments rejected")
                # Crash/disconnect cannot create a fresh budget or reuse a partial result as healthy.
                receipt = {
                    "type": "metric_wait",
                    "phase": "terminal",
                    "step_id": step_id,
                    "observed_at": _now_iso(),
                    "ok": False,
                    "status": "UNOBSERVABLE",
                    "error": "prior wait was interrupted; no restart or rebase",
                    "binding": started["binding"],
                }
                _append_record(receipt)
                return json.dumps(receipt, ensure_ascii=False)
            if error := order_error(steps, step_id, records):
                return _reject_approval(error, step_id)
            context = json.loads(
                observation_context_path_for_token(os.environ.get(EXECUTION_TOKEN_ENV, "")).read_text()
            )
            bound = bind_request(request, records, context, os.environ.get(EXECUTION_ID_ENV, ""), evaluate_command)
            if timestamp(bound["anchor"]["ended_at"]) > time.time():
                raise ValueError("action ended_at is in the future")
            deadline = min(_observation_control(), time.time() + max_wait_seconds)
            bound.update(request=request, deadline=utc(deadline))
            if not _append_record(
                {
                    "type": "metric_wait",
                    "phase": "started",
                    "step_id": step_id,
                    "observed_at": _now_iso(),
                    "binding": bound,
                }
            ):
                raise ValueError("cannot persist anchor before polling")

            def append(receipt: dict) -> None:
                if not _append_record(receipt):
                    raise ValueError("cannot persist observation receipt")

            budget = ObservationBudget(deadline, _observation_control)
            check_scope = None
            if deployment_step_id:
                from headless_codex.services.service_deployment import approved_action, poll_deployment

                deployment = next(s["deployment_wait"] for s in steps if s["step_id"] == deployment_step_id)
                action = approved_action(steps, deployment, records, os.environ.get(EXECUTION_ID_ENV, ""))
                guard = next(
                    s["ecs_service_precondition"] for s in steps if s["step_id"] == deployment["action_step_id"]
                )

                def check_scope(current_budget: ObservationBudget) -> None:
                    def persist_check(receipt: dict) -> None:
                        receipt.update(
                            type="deployment_continuity",
                            step_id=step_id,
                            execution_id=os.environ.get(EXECUTION_ID_ENV, ""),
                        )
                        receipt.pop("first_converged_at", None)
                        append(receipt)

                    poll_deployment(
                        deployment,
                        guard,
                        action,
                        current_budget,
                        lambda c, b: json.loads(
                            _run_command(step_id, c, "Approved deployment continuity", budget=b, ecs_observation=True)
                        ),
                        persist_check,
                        wait_for_convergence=False,
                    )

            result = poll_fixed_metrics(
                bound,
                budget,
                lambda command, budget: json.loads(
                    _run_command(step_id, command, "Fixed post-action metric observation", budget=budget)
                ),
                append,
                check_scope=check_scope,
            )
            return json.dumps(result, ensure_ascii=False)
    except (OSError, ValueError, TypeError, KeyError, ObservationStoppedError) as exc:
        if request is not None and approved_request is not None and same_request(request, approved_request):
            receipt = {
                "type": "metric_wait",
                "phase": "terminal",
                "step_id": step_id,
                "observed_at": _now_iso(),
                "ok": False,
                "status": "UNOBSERVABLE",
                "error": str(exc),
                "binding": {"request": request},
            }
            _append_record(receipt)
            return json.dumps(receipt, ensure_ascii=False)
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@mcp.tool()
def wait_for_service_deployment(step_id: str) -> str:
    """Observe the exact approved deployment; only a step ID is caller-controlled.

    The first healthy server convergence timestamp anchors a later metric_wait.
    A duplicate returns its existing receipt; an interrupted wait never restarts.
    """
    from headless_codex.services.service_deployment import approved_action, poll_deployment

    request = None
    binding = None
    try:
        steps = validate_steps(_approved_playbook())
        step = next((s for s in steps if s["step_id"] == step_id), {})
        request = step.get("deployment_wait")
        if not request:
            return _reject_approval("step is not an approved deployment_wait", step_id)
        path = _evidence_file()
        if path is None:
            raise ValueError("missing execution context")
        with (path.parent / "execution-operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return json.dumps({"ok": False, "error": "an execution operation is already running"})
            records = _read_records()
            if records is None:
                raise ValueError("missing execution evidence")
            previous = [r for r in records if r.get("type") == "deployment_wait" and r.get("step_id") == step_id]
            terminal = next((r for r in previous if r.get("phase") == "terminal"), None)
            if terminal:
                if not same_request(terminal.get("binding", {}).get("request", {}), request):
                    return _reject_approval("deployment request changed", step_id)
                return json.dumps(terminal)
            if previous:
                binding = previous[0].get("binding")
                raise ValueError("prior deployment wait interrupted; no new deadline")
            if error := order_error(steps, step_id, records):
                return _reject_approval(error, step_id)
            action_step = next(s for s in steps if s["step_id"] == request["action_step_id"])
            action = approved_action(steps, request, records, os.environ.get(EXECUTION_ID_ENV, ""))
            deadline = min(_observation_control(), time.time() + request["max_wait_seconds"])
            binding = {"request": request, "deadline": utc(deadline), "action_record": action["ended_at"]}

            def append(receipt: dict) -> None:
                receipt.update(step_id=step_id, execution_id=os.environ.get(EXECUTION_ID_ENV, ""))
                if not _append_record(receipt):
                    raise ValueError("cannot persist deployment receipt")

            append({"type": "deployment_wait", "phase": "started", "observed_at": _now_iso(), "binding": binding})
            budget = ObservationBudget(deadline, _observation_control)
            result = poll_deployment(
                request,
                action_step["ecs_service_precondition"],
                action,
                budget,
                lambda c, b: json.loads(
                    _run_command(step_id, c, "Approved deployment convergence", budget=b, ecs_observation=True)
                ),
                append,
            )
            return json.dumps(result)
    except (OSError, ValueError, KeyError, TypeError, ObservationStoppedError) as exc:
        if request is None:
            return _reject_approval(str(exc), step_id)
        receipt = {
            "type": "deployment_wait",
            "phase": "terminal",
            "step_id": step_id,
            "execution_id": os.environ.get(EXECUTION_ID_ENV, ""),
            "observed_at": _now_iso(),
            "ok": False,
            "status": "UNOBSERVABLE",
            "error": str(exc),
            "binding": binding or {"request": request},
        }
        _append_record(receipt)
        return json.dumps(receipt)


def _metric_wait_blocks_success(records: list[dict], step_id: str | None = None) -> bool:
    waits = [r for r in records if r.get("type") == "metric_wait" and (step_id is None or r.get("step_id") == step_id)]
    return metric_wait_blocks_success(waits)


def _outcome_evidence(records: list[dict]) -> ExecutionEvidence:
    """Replay the approved steps for recording guards using the final judge's contract."""
    return assemble_evidence(
        records,
        execution_id=os.environ.get(EXECUTION_ID_ENV, ""),
        rca_id="",
        engine="headless-codex",
        playbook=_approved_playbook(),
    )


@mcp.tool()
def record_step_outcome(
    step_id: str,
    success_criteria: str,
    observation: str,
    criteria_met: StrictBool,
    failure_class: str = "",
    manual_action_required: StrictBool = False,
) -> str:
    """한 절차의 관측 결과를 증거에 기록한다.

    Args:
        step_id: 플레이북 실행 절차의 식별자.
        success_criteria: 이 절차가 성공을 판정하는 기준 — 플레이북에 적힌 그대로.
        observation: 그 기준을 관측한 결과. 어떤 지표가 어떤 값이었는지 쓴다.
        criteria_met: 관측이 기준을 만족했는지. 관측하지 못했으면 false 로 둔다.
        failure_class: 만족하지 못한 경우의 분류.
        manual_action_required: 이 절차가 사람의 조치로 남아야 하는지.
    """
    step_error = _validate_step_id(step_id)
    if step_error:
        return json.dumps({"ok": False, "error": step_error}, ensure_ascii=False)
    if type(criteria_met) is not bool or type(manual_action_required) is not bool:
        return json.dumps({"ok": False, "error": "criteria_met and manual_action_required must be booleans"})
    if criteria_met and (not isinstance(observation, str) or not observation.strip()):
        return json.dumps({"ok": False, "error": "criteria_met=true requires a nonblank observation"})
    if criteria_met and manual_action_required:
        return json.dumps({"ok": False, "error": "criteria_met=true conflicts with manual_action_required"})

    approved_criteria = _approved_success_criteria().get(step_id.strip())
    if approved_criteria is None:
        return json.dumps(
            {"ok": False, "error": "approved success_criteria is unavailable for this step"},
            ensure_ascii=False,
        )

    records = _read_records()
    if records is None:
        return json.dumps({"ok": False, "error": "missing execution context"}, ensure_ascii=False)
    normalized_step_id = step_id.strip()
    if criteria_met and _metric_wait_blocks_success(records, normalized_step_id):
        return json.dumps({"ok": False, "error": "fixed metric wait is failed, incomplete or unobservable"})
    has_attempt = any(
        record.get("type") in {"attempt", "metric_wait", "deployment_wait"}
        and record.get("step_id") == normalized_step_id
        for record in records
    )
    if not has_attempt:
        return json.dumps(
            {
                "ok": False,
                "error": "record_step_outcome requires a preceding run_playbook_command attempt",
                "missing_attempt_step_ids": [normalized_step_id],
            },
            ensure_ascii=False,
        )

    if success_criteria != approved_criteria:
        return json.dumps(
            {
                "ok": False,
                "error": "success_criteria does not exactly match the approved playbook",
            },
            ensure_ascii=False,
        )

    if criteria_met:
        reason = step_execution_blocker(_outcome_evidence(records).step(normalized_step_id))
        if reason:
            return json.dumps({"ok": False, "error": reason}, ensure_ascii=False)

    ok = _append_record(
        {
            "type": "step_outcome",
            "step_id": step_id.strip(),
            "success_criteria": approved_criteria,
            "observation": redact(observation),
            "criteria_met": criteria_met,
            "failure_class": str(parse_failure_class(failure_class)) if failure_class else None,
            "manual_action_required": manual_action_required,
        }
    )
    if not ok:
        return json.dumps({"ok": False, "error": "missing execution context"}, ensure_ascii=False)
    return json.dumps({"ok": True}, ensure_ascii=False)


@mcp.tool()
def record_resolution(observation: str, resolved: StrictBool, unobservable_reason: str = "") -> str:
    """이슈 해소 여부의 관측 결과를 기록한다.

    관측으로 확정할 수 없으면 `resolved=false` 와 함께 `unobservable_reason` 을 남긴다.
    서버는 관측 실패를 해결로 추정하지 않는다.

    Args:
        observation: 해소 여부를 판정한 관측 내용. 어떤 지표를 어떤 구간에서 보았고
                     그 값이 무엇이었는지 쓴다.
        resolved: 관측이 해소를 확인했는지.
        unobservable_reason: 관측으로 확정할 수 없었던 이유.
    """
    if type(resolved) is not bool:
        return json.dumps({"ok": False, "error": "resolved must be a boolean"})
    if resolved and (not isinstance(observation, str) or not observation.strip()):
        return json.dumps(
            {"ok": False, "error": "resolved=true requires a nonblank observation"},
            ensure_ascii=False,
        )
    if resolved and unobservable_reason:
        return json.dumps({"ok": False, "error": "resolved=true conflicts with unobservable_reason"})

    record = {
        "type": "resolution",
        "observation": redact(observation),
        "resolved": resolved,
        "unobservable_reason": redact(unobservable_reason),
    }

    if resolved:
        approved_step_ids = _approved_step_ids()
        if not approved_step_ids:
            return json.dumps(
                {"ok": False, "error": "approved step list is unavailable"},
                ensure_ascii=False,
            )
        records = _read_records()
        if records is None:
            return json.dumps({"ok": False, "error": "missing execution context"}, ensure_ascii=False)
        if _metric_wait_blocks_success(records):
            return json.dumps({"ok": False, "error": "fixed metric wait is failed, incomplete or unobservable"})
        attempted_step_ids = {
            record.get("step_id")
            for record in records
            if record.get("type") in {"attempt", "metric_wait", "deployment_wait"}
        }
        outcome_step_ids = {record.get("step_id") for record in records if record.get("type") == "step_outcome"}
        missing_attempt_step_ids = [step_id for step_id in approved_step_ids if step_id not in attempted_step_ids]
        missing_outcome_step_ids = [step_id for step_id in approved_step_ids if step_id not in outcome_step_ids]
        if missing_attempt_step_ids or missing_outcome_step_ids:
            return json.dumps(
                {
                    "ok": False,
                    "error": ("resolved=true requires attempt and outcome evidence for every approved playbook step"),
                    "missing_attempt_step_ids": missing_attempt_step_ids,
                    "missing_outcome_step_ids": missing_outcome_step_ids,
                },
                ensure_ascii=False,
            )

        if any(step_id not in _approved_success_criteria() for step_id in approved_step_ids):
            return json.dumps({"ok": False, "error": "approved success_criteria is unavailable"})
        verdict = judge_resolution(_outcome_evidence([*records, record]), agent_succeeded=True)
        if verdict.state is not ExecutionState.RESOLVED:
            return json.dumps({"ok": False, "error": verdict.reason}, ensure_ascii=False)

    ok = _append_record(record)
    if not ok:
        return json.dumps({"ok": False, "error": "missing execution context"}, ensure_ascii=False)
    return json.dumps({"ok": True}, ensure_ascii=False)
