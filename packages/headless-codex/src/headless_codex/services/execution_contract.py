"""Enforce the immutable approved runbook independently of the model's narrative."""

from __future__ import annotations

import hashlib
import json

from headless_codex.services.command_gate import evaluate_command
from headless_codex.services.post_action_metrics import normalize_request


def command_digest(command: str) -> str:
    """Retain exact command identity even when the audit display must redact arguments."""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def command_identity(command: str) -> str:
    """Recognize the same AWS argv across quoting/spacing changes when guarding write replay."""
    return command_digest(json.dumps(evaluate_command(command).argv, ensure_ascii=False))


def approved_wait(step: dict) -> dict:
    """Apply only existing tool defaults; reject unknown fields and caller-owned step IDs."""
    value = step["metric_wait"]
    required = {"action_step_id", "metrics", "failure_alarm_name", "region"}
    optional = {"max_wait_seconds", "latency_alarm_name", "completed_work_evidence"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError("metric_wait must contain the existing wait arguments, excluding step_id")
    return normalize_request(
        step["step_id"],
        value["action_step_id"],
        value["metrics"],
        value["failure_alarm_name"],
        value.get("latency_alarm_name", ""),
        value["region"],
        value.get("max_wait_seconds", 300),
        value.get("completed_work_evidence"),
    )


def same_request(left: dict, right: dict) -> bool:
    """Compare JSON values without Python's boolean/integer equality coercion."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def validate_steps(playbook: dict) -> list[dict]:
    """Fail closed for legacy plans, ambiguous operations and invalid wait references."""
    steps = playbook.get("execution_steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("approved playbook declares no execution steps")
    ids: set[str] = set()
    normalized: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("approved playbook has invalid execution steps")
        step_id = step.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip() or step_id != step_id.strip() or step_id in ids:
            raise ValueError("approved playbook has invalid or duplicate execution step IDs")
        criterion = step.get("success_criteria")
        if not isinstance(criterion, str) or not criterion.strip():
            raise ValueError("approved playbook has a missing or invalid success criterion")
        commands = step.get("commands")
        has_commands = isinstance(commands, list) and bool(commands)
        has_wait = step.get("metric_wait") is not None
        if has_commands == has_wait or ("commands" in step and not isinstance(commands, list)):
            raise ValueError("each approved step requires nonempty commands XOR metric_wait; new approval required")
        if has_commands:
            if any(not isinstance(command, str) or not command.strip() for command in commands):
                raise ValueError("approved commands must be nonblank AWS CLI strings")
            normalized.append({key: value for key, value in step.items() if key != "metric_wait"})
        else:
            request = approved_wait(step)
            if request["action_step_id"] not in ids:
                raise ValueError("metric_wait requires an earlier approved action_step_id")
            normalized.append({key: value for key, value in step.items() if key != "commands"})
        ids.add(step_id)
    return normalized


def is_read(command: str) -> bool:
    """Permit repeated observations conservatively; unknown operations count as writes."""
    verdict = evaluate_command(command)
    return verdict.allowed and (
        verdict.operation.startswith(("describe-", "list-", "get-", "head-", "lookup-"))
        or (verdict.service, verdict.operation) == ("logs", "filter-log-events")
    )


def command_records(step: dict, records: list[dict], index: int) -> list[dict]:
    """Select server command attempts by approved position and unredacted identity digest."""
    digest = command_digest(step["commands"][index])
    return [
        record
        for record in records
        if record.get("type") == "attempt"
        and record.get("step_id") == step["step_id"]
        and record.get("command_index") == index
        and record.get("command_digest") == digest
        and not record.get("internal_observation")
    ]


def _successful(record: dict) -> bool:
    return record.get("succeeded") is True and not record.get("blocked") and str(record.get("exit_status")) == "0"


def _prerequisite_complete(attempts: list[dict]) -> bool:
    """A policy block remains manual; an ordinary failure cannot authorize a later write."""
    return bool(attempts) and (_successful(attempts[-1]) or attempts[-1].get("blocked") is True)


def step_attempted(step: dict, records: list[dict]) -> bool:
    """Advancing requires all commands to have been attempted, or a terminal wait receipt."""
    if step.get("metric_wait") is not None:
        return any(
            r.get("type") == "metric_wait" and r.get("step_id") == step["step_id"] and r.get("phase") == "terminal"
            for r in records
        )
    return all(command_records(step, records, index) for index in range(len(step["commands"])))


def order_error(steps: list[dict], step_id: str, records: list[dict]) -> str | None:
    """Reject skipped steps and returning to an earlier step after execution advanced."""
    ids = [step["step_id"] for step in steps]
    index = ids.index(step_id)
    if any(not step_attempted(step, records) for step in steps[:index]):
        return "approved step order requires every preceding command/wait to be attempted"
    if any(
        r.get("step_id") in ids[index + 1 :] and r.get("type") in {"attempt", "command_started", "metric_wait"}
        for r in records
    ):
        return "execution has already advanced beyond this approved step"
    return None


def authorize_command(steps: list[dict], step_id: str, command: str, records: list[dict]) -> int:
    """Select the next exact command, allowing only same-position transient retries or reads."""
    step = next(s for s in steps if s["step_id"] == step_id)
    if step.get("metric_wait") is not None or command not in step["commands"]:
        raise ValueError("command does not exactly match approved commands; new runbook approval required")
    if error := order_error(steps, step_id, records):
        raise ValueError(error)
    commands = step["commands"]
    next_index = next((i for i in range(len(commands)) if not command_records(step, records, i)), len(commands))
    if next_index < len(commands) and commands[next_index] == command:
        index = next_index
    elif next_index and commands[next_index - 1] == command:
        index = next_index - 1
        previous = command_records(step, records, index)[-1]
        if previous.get("blocked") or previous.get("failure_class") not in (
            None,
            "TRANSIENT",
            "THROTTLED",
            "TIMEOUT",
            "UNKNOWN",
        ):
            raise ValueError("only an identical command with a temporary failure may be retried")
    else:
        raise ValueError("command is not next in the approved command order")
    if not is_read(command):
        prerequisites = [(step, i) for i in range(index)]
        for prior in steps[: steps.index(step)]:
            prerequisites.extend((prior, i) for i in range(len(prior.get("commands", []))))
        if any(not _prerequisite_complete(command_records(prior, records, i)) for prior, i in prerequisites):
            raise ValueError("a preceding approved command failed; its prerequisite is not complete")
        identity = command_identity(command)
        same_write = [
            r for r in records if r.get("command_identity", command_identity(r.get("command", ""))) == identity
        ]
        if any(r.get("type") == "attempt" and r.get("succeeded") is True for r in same_write):
            raise ValueError("successful write must not be executed again")
        starts = sum(r.get("type") == "command_started" for r in same_write)
        finishes = sum(r.get("type") == "attempt" for r in same_write)
        if starts > finishes:
            raise ValueError("prior write has an unknown result; do not repeat it")
    return index


def step_contract_error(step: dict, records: list[dict]) -> str | None:
    """Require every approved command to succeed before accepting an observed outcome."""
    if any(r.get("type") == "approval_rejection" for r in records):
        return "approved scope or order was rejected; new runbook approval required"
    if step.get("metric_wait") is not None:
        waits = [r for r in records if r.get("type") == "metric_wait" and r.get("step_id") == step["step_id"]]
        terminal = [r for r in waits if r.get("phase") == "terminal"]
        if not terminal or any(r.get("status") != "HEALTHY" for r in terminal):
            return "approved metric_wait has no healthy terminal observation"
        request = approved_wait(step)
        if any(not same_request(r.get("binding", {}).get("request", {}), request) for r in terminal):
            return "metric_wait evidence does not match approved arguments"
        return None
    for index in range(len(step["commands"])):
        attempts = command_records(step, records, index)
        starts = [
            r
            for r in records
            if r.get("type") == "command_started"
            and r.get("step_id") == step["step_id"]
            and r.get("command_index") == index
            and r.get("command_digest") == command_digest(step["commands"][index])
        ]
        if len(starts) > len([r for r in attempts if not r.get("blocked")]):
            return f"approved command {index + 1} has an unfinished attempt"
        if not attempts or not _successful(attempts[-1]):
            return f"approved command {index + 1} has no successful attempt"
    return None
