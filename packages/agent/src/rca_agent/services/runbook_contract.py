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
    deployment = step.get("deployment_wait")
    if not isinstance(commands, list) or sum((bool(commands), wait is not None, deployment is not None)) != 1:
        raise ValueError("step requires commands XOR metric_wait XOR deployment_wait")
    if step.get("ecs_service_precondition") is not None:
        validate_precondition(step)
    if deployment is not None:
        validate_deployment_wait(deployment)
        return
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
    required = {"metrics", "failure_alarm_name", "region"}
    anchors = set(wait) & {"action_step_id", "deployment_step_id"}
    if len(anchors) != 1:
        raise ValueError("metric_wait requires action_step_id XOR deployment_step_id")
    required |= anchors
    optional = {"max_wait_seconds", "latency_alarm_name", "completed_work_evidence"}
    if not required <= set(wait) or set(wait) - required - optional:
        raise ValueError("metric_wait requires fixed tool arguments without step_id")
    for key in (*anchors, "failure_alarm_name", "region"):
        if not _text(wait[key]):
            raise ValueError(f"metric_wait requires fixed {key}")
    if not _REGION.fullmatch(wait["region"]):
        raise ValueError("metric_wait region is invalid")
    duration = wait.get("max_wait_seconds", 900)
    if type(duration) is not int or not 1 <= duration <= 900:
        raise ValueError("max_wait_seconds must be between 1 and 900")
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
    validate_completed_work_reference(wait.get("completed_work_evidence"))


def validate_completed_work_reference(reference: object) -> None:
    """Reject references the evidence binder cannot interpret before approval.

    The server-owned context has one fixed pointer. Numeric references retain
    their observed-record JSON pointers; record existence and provenance are
    checked later when those actual execution records are available.
    """
    if reference is None:
        return
    if not isinstance(reference, dict) or set(reference) != {"record_index", "json_pointer"}:
        raise ValueError("completed_work_evidence requires record_index and json_pointer")
    index, pointer = reference["record_index"], reference["json_pointer"]
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("source descriptor requires JSON pointer")
    if index == "approved_context":
        if pointer != "/playbook/rollback_context/write_accounting":
            raise ValueError("completed-work evidence requires reader-owned normal context")
    elif type(index) is not int or index < 0:
        raise ValueError("completed-work source reference unavailable")


def validate_runbook(steps: list[dict]) -> None:
    """Reject incomplete fixed operations and causal recovery chains before publication."""
    seen: set[str] = set()
    stop_steps: set[str] = set()
    deployment_steps: dict[str, dict] = {}
    prior: dict[str, dict] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not _text(step.get("step_id")):
            raise ValueError(f"execution step {index + 1} requires a nonempty step_id")
        if step["step_id"] in seen:
            raise ValueError(f"execution step {index + 1} has a duplicate step_id")
        if not _text(step.get("action")):
            raise ValueError(f"execution step {index + 1} requires a nonempty action")
        if not _text(step.get("success_criteria")):
            raise ValueError(f"execution step {index + 1} requires nonempty success_criteria")
        validate_step_operation(step)
        if (step.get("metric_wait") or {}).get("action_step_id") and step["metric_wait"][
            "action_step_id"
        ] not in stop_steps:
            raise ValueError("metric_wait action_step_id must reference a prior aws ecs stop-task step")
        if any(_command_parts(command)[0][:3] == ["aws", "ecs", "stop-task"] for command in step.get("commands", [])):
            stop_steps.add(step["step_id"])
        if wait := step.get("deployment_wait"):
            action = prior.get(wait["action_step_id"], {})
            validate_deployment_pair(action, wait)
            if wait["action_step_id"] in {d["action_step_id"] for d in deployment_steps.values()}:
                raise ValueError("duplicate deployment wait for action")
            deployment_steps[step["step_id"]] = wait
        reference = (step.get("metric_wait") or {}).get("deployment_step_id")
        if reference and (
            reference not in deployment_steps or step["metric_wait"]["region"] != deployment_steps[reference]["region"]
        ):
            raise ValueError("metric_wait requires a prior same-region deployment_wait")
        seen.add(step["step_id"])
        prior[step["step_id"]] = step
    for step in steps:
        if (
            step.get("ecs_service_precondition")
            and sum(d["action_step_id"] == step["step_id"] for d in deployment_steps.values()) != 1
        ):
            raise ValueError("guarded rollback requires exactly one following deployment_wait")
    validate_recovery_sequence(steps)


def validate_recovery_sequence(steps: list[dict]) -> None:
    """Require recovery metrics after each guarded deployment, never a stop-task substitute.

    Generation, approval and execution can share this structural check without
    inferring success from a command's prose or adding requirements to legacy actions.
    """
    for index, action in enumerate(steps):
        if not action.get("ecs_service_precondition"):
            continue
        followers = [
            (position, step)
            for position, step in enumerate(steps)
            if (step.get("deployment_wait") or {}).get("action_step_id") == action["step_id"]
        ]
        if len(followers) != 1 or followers[0][0] <= index:
            raise ValueError("guarded rollback requires exactly one following deployment_wait")
        position, deployment = followers[0]
        if not any(
            (step.get("metric_wait") or {}).get("deployment_step_id") == deployment["step_id"]
            and step["metric_wait"].get("region") == deployment["deployment_wait"]["region"]
            for step in steps[position + 1 :]
        ):
            raise ValueError("guarded rollback requires subsequent same-region recovery metric_wait")


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
    for field in ("ecs_service_precondition", "deployment_wait", "metric_wait"):
        if step.get(field) is None:
            continue
        lines += [
            f"**{field}**",
            "",
            "```json",
            json.dumps(step[field], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    return lines


SERVICE_SETTING_DEFAULTS = {
    "deploymentConfiguration": {},
    "networkConfiguration": {},
    "capacityProviderStrategy": [],
    "launchType": None,
    "platformVersion": None,
    "schedulingStrategy": "REPLICA",
}
SCOPE_FIELDS = {"account_id", "region", "cluster", "service", "container_name", "desired_count"}


def validate_scope(value: dict) -> None:
    if not all(_text(value.get(key)) for key in SCOPE_FIELDS - {"desired_count"}):
        raise ValueError("deployment requires fixed scope")
    if not re.fullmatch(r"[0-9]{12}", value["account_id"]) or not _REGION.fullmatch(value["region"]):
        raise ValueError("invalid deployment account/region")
    prefix = f"arn:aws:ecs:{value['region']}:{value['account_id']}:"
    for key, kind in (("cluster", "cluster/"), ("service", "service/")):
        if not value[key].startswith(prefix + kind):
            raise ValueError("deployment requires same-account/region ARN scope")
    cluster = value["cluster"].split("cluster/", 1)[1]
    if not value["service"].startswith(prefix + "service/" + cluster + "/"):
        raise ValueError("service must belong to approved cluster")
    if type(value["desired_count"]) is not int or value["desired_count"] < 1:
        raise ValueError("deployment requires positive desired_count")


def validate_definition(value: dict, definition: str, digest: str) -> None:
    prefix = f"arn:aws:ecs:{value['region']}:{value['account_id']}:task-definition/"
    if not _text(value.get(definition)) or not value[definition].startswith(prefix):
        raise ValueError("task definition must belong to approved account/region")
    if not re.fullmatch(r"[A-Za-z0-9_-]+:[1-9][0-9]*", value[definition][len(prefix) :]):
        raise ValueError("task definition must have an explicit revision")
    if not isinstance(value.get(digest), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value[digest]):
        raise ValueError("immutable image digest required")


def rollback_argv(command: str) -> dict:
    argv = shlex.split(command)
    if argv[:3] != ["aws", "ecs", "update-service"] or len(argv) != 11:
        raise ValueError("guarded rollback requires ordinary update-service argv with task-definition only")
    options = dict(zip(argv[3::2], argv[4::2], strict=True))
    if len(options) != 4 or set(options) != {"--cluster", "--service", "--task-definition", "--region"}:
        raise ValueError("guarded rollback permits only cluster/service/task-definition/region")
    return options


def validate_precondition(step: dict) -> None:
    value = step["ecs_service_precondition"]
    fields = SCOPE_FIELDS | {
        "expected_task_definition",
        "expected_image_digest",
        "expected_deployment_id",
        "service_settings",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("ecs_service_precondition requires exact typed scope")
    validate_scope(value)
    if not _text(value["expected_deployment_id"]):
        raise ValueError("expected deployment identity required")
    validate_definition(value, "expected_task_definition", "expected_image_digest")
    settings = value["service_settings"]
    if not isinstance(settings, dict) or set(settings) != set(SERVICE_SETTING_DEFAULTS):
        raise ValueError("service_settings requires the complete normalized settings snapshot")
    if not isinstance(settings["deploymentConfiguration"], dict) or not isinstance(
        settings["networkConfiguration"], dict
    ):
        raise ValueError("invalid deployment/network settings")
    if not isinstance(settings["capacityProviderStrategy"], list) or settings["schedulingStrategy"] != "REPLICA":
        raise ValueError("only replica service rollback is supported")
    for key in ("launchType", "platformVersion"):
        if settings[key] is not None and not _text(settings[key]):
            raise ValueError("invalid service setting")
    commands = step.get("commands")
    if not isinstance(commands, list) or len(commands) != 1:
        raise ValueError("guarded rollback requires exactly one command")
    options = rollback_argv(commands[0])
    if any(options["--" + key] != value[key] for key in ("cluster", "service", "region")):
        raise ValueError("rollback command scope differs from precondition")
    if options["--task-definition"] == value["expected_task_definition"]:
        raise ValueError("rollback must change the fault definition")


def validate_deployment_wait(value: dict) -> None:
    fields = SCOPE_FIELDS | {"action_step_id", "task_definition", "image_digest", "max_wait_seconds"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("deployment_wait requires exact fixed arguments")
    validate_scope(value)
    validate_definition(value, "task_definition", "image_digest")
    if (
        not _text(value["action_step_id"])
        or type(value["max_wait_seconds"]) is not int
        or not 1 <= value["max_wait_seconds"] <= 900
    ):
        raise ValueError("deployment wait requires prior action and max_wait_seconds in 1..900")


def validate_deployment_pair(action: dict, wait: dict) -> None:
    if "ecs_service_precondition" not in action:
        raise ValueError("deployment_wait requires prior guarded rollback; guard cannot be stripped")
    validate_precondition(action)
    guard = action["ecs_service_precondition"]
    if any(guard[k] != wait[k] for k in SCOPE_FIELDS):
        raise ValueError("deployment wait scope differs from guarded rollback")
    if rollback_argv(action["commands"][0])["--task-definition"] != wait["task_definition"]:
        raise ValueError("deployment target differs from approved command")
    if guard["expected_image_digest"] == wait["image_digest"]:
        raise ValueError("normal and fault image digests must differ")
