"""Validate newly generated approval inputs; legacy reads never infer commands.

This is an artifact completeness check. Execution authorization remains in the
execution worker. Keep the two independently deployed engines' copies in parity.
"""

from __future__ import annotations

import json
import re
import shlex
from functools import lru_cache

from botocore import xform_name
from botocore.session import Session

_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d+")
_UNFIXED = re.compile(r"\$(?!\.[A-Za-z_])|`|<[^>]+>|\{\{|\b(?:TODO|TBD|PLACEHOLDER)\b", re.I)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not _UNFIXED.search(value)


def _command_parts(command: str) -> tuple[list[str], str | None]:
    """Parse quoted arguments and the region option without changing stored input."""
    argv = shlex.split(command)
    operation_argv = []
    region = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--region":
            index += 1
            region = argv[index] if index < len(argv) else None
        elif token.startswith("--region="):
            region = token.split("=", 1)[1]
        else:
            operation_argv.append(token)
        index += 1
    return operation_argv, region


@lru_cache(maxsize=64)
def _required_cli_arguments(service: str, operation: str) -> tuple[str, ...]:
    # Read the installed AWS service model; this makes no AWS calls and avoids
    # accepting incomplete commands for services beyond the local ECS examples.
    try:
        model = Session().get_service_model(service)
        name = next(name for name in model.operation_names if xform_name(name, "-") == operation)
        shape = model.operation_model(name).input_shape
    except Exception as exc:
        raise ValueError("command must name a known AWS service operation") from exc
    return tuple("--" + xform_name(name, "-") for name in shape.required_members) if shape else ()


def validate_step_operation(step: dict) -> None:
    commands = step.get("commands", [])
    wait = step.get("metric_wait")
    if not isinstance(commands, list) or bool(commands) == (wait is not None):
        raise ValueError("step requires commands XOR metric_wait")
    if commands:
        for command in commands:
            if not _text(command) or any(c in command for c in ("\n", "\r", "\x00")):
                raise ValueError("commands must be complete fixed AWS CLI strings")
            argv, region = _command_parts(command)
            if len(argv) < 3 or argv[0] != "aws" or argv[1].startswith("-") or argv[2].startswith("-"):
                raise ValueError("command must start with aws service operation")
            if any(t in {";", "&&", "||", "|", ">", "<"} or t.startswith("file://") for t in argv):
                raise ValueError("command must be standalone without external inputs")
            if not region or not _REGION.fullmatch(region):
                raise ValueError("command requires a fixed --region")
            # Targeted control operations cannot rely on default cluster/target selection.
            required = {
                ("ecs", "stop-task"): ("--cluster", "--task"),
                ("ecs", "describe-tasks"): ("--cluster", "--tasks"),
                ("ecs", "update-service"): ("--cluster", "--service"),
                ("ecs", "describe-services"): ("--cluster", "--services"),
            }.get(tuple(argv[1:3]), ())
            required += _required_cli_arguments(argv[1], argv[2])
            for option in required:
                values = [t.split("=", 1)[1] for t in argv if t.startswith(option + "=")]
                if option in argv and argv.index(option) + 1 < len(argv):
                    values.append(argv[argv.index(option) + 1])
                if not values or not all(_text(v) and not v.startswith("-") for v in values):
                    raise ValueError(f"command requires fixed argument {option}")
        return
    if not isinstance(wait, dict):
        raise ValueError("metric_wait must be an object")
    required = {"action_step_id", "metrics", "failure_alarm_name", "region"}
    optional = {"max_wait_seconds", "latency_alarm_name", "completed_work_evidence"}
    if not required <= set(wait) or set(wait) - required - optional:
        raise ValueError("metric_wait requires fixed tool arguments without step_id")
    for key in ("action_step_id", "failure_alarm_name", "region"):
        if not _text(wait[key]):
            raise ValueError(f"metric_wait requires fixed {key}")
    if not _REGION.fullmatch(wait["region"]):
        raise ValueError("metric_wait region is invalid")
    duration = wait.get("max_wait_seconds", 300)
    if type(duration) is not int or not 1 <= duration <= 300:
        raise ValueError("max_wait_seconds must be between 1 and 300")
    metrics = wait["metrics"]
    if not isinstance(metrics, dict) or set(metrics) not in (
        {"attempts", "failures"},
        {"attempts", "failures", "latency"},
    ):
        raise ValueError("metrics requires attempts/failures and optional latency")
    if bool(wait.get("latency_alarm_name")) != ("latency" in metrics):
        raise ValueError("latency metric and alarm must be supplied together")
    if wait.get("latency_alarm_name") and not _text(wait["latency_alarm_name"]):
        raise ValueError("latency alarm must be fixed")
    for metric in metrics.values():
        if not isinstance(metric, dict) or set(metric) != {"namespace", "metric_name", "dimensions"}:
            raise ValueError("metric requires namespace, metric_name, dimensions")
        if not _text(metric["namespace"]) or not _text(metric["metric_name"]):
            raise ValueError("metric coordinates must be fixed")
        dims = metric["dimensions"]
        if (
            not isinstance(dims, dict)
            or not 1 <= len(dims) <= 30
            or not all(_text(k) and _text(v) for k, v in dims.items())
        ):
            raise ValueError("metric requires observed nonempty dimensions")
    base = metrics["failures"]
    if any(m["namespace"] != base["namespace"] or m["dimensions"] != base["dimensions"] for m in metrics.values()):
        raise ValueError("metrics must share namespace and dimensions")
    if len({m["metric_name"] for m in metrics.values()}) != len(metrics):
        raise ValueError("metric names must be distinct")
    if wait.get("completed_work_evidence") is not None and not isinstance(wait["completed_work_evidence"], dict):
        raise ValueError("completed_work_evidence must be an object")


def validate_runbook(steps: list[dict]) -> None:
    seen: set[str] = set()
    stop_steps: set[str] = set()
    for step in steps:
        if not isinstance(step, dict) or not _text(step.get("step_id")):
            raise ValueError("execution step requires step_id")
        if step["step_id"] in seen or not _text(step.get("action")) or not _text(step.get("success_criteria")):
            raise ValueError("execution step requires unique ID, action and success criteria")
        validate_step_operation(step)
        if step.get("metric_wait") and step["metric_wait"]["action_step_id"] not in stop_steps:
            raise ValueError("metric_wait action_step_id must reference a prior aws ecs stop-task step")
        if any(_command_parts(command)[0][:3] == ["aws", "ecs", "stop-task"] for command in step.get("commands", [])):
            stop_steps.add(step["step_id"])
        seen.add(step["step_id"])


def render_step_operation(step: dict) -> list[str]:
    """Render exact structured values without rewriting or truncating approval input."""
    lines = [
        "**commands**",
        "",
        "```json",
        json.dumps(step.get("commands", []), ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    if step.get("metric_wait") is not None:
        lines += [
            "**metric_wait**",
            "",
            "```json",
            json.dumps(step["metric_wait"], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    return lines
