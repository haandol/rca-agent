"""Bounded ECS observation. No caller-selected operations or full AWS responses."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from headless_codex.ports.dto.models import _alarm_arn_region, _region_partition, parse_alarm

_NAME = r"[a-zA-Z0-9_-]{1,255}"
_TASK_ID = r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
_RESOURCES = {
    "cluster": re.compile(rf"cluster/{_NAME}"),
    "task": re.compile(rf"task/(?:{_NAME}/)?{_TASK_ID}"),
    "task-definition": re.compile(rf"task-definition/{_NAME}:[1-9][0-9]*"),
}
_SENSITIVE = re.compile(
    r"(?i)(secret|password|passwd|token|credential|authorization|api.?key|private.?key|"
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|://[^/\s]+@|-----BEGIN|eyJ[a-zA-Z0-9_-]+\.)"
)
# Unrecognized free-form tag values are deliberately redacted. Keep only useful
# ownership/lifecycle labels; even these pass the sensitive-value filter.
_TAG_KEYS = {
    "name",
    "owner",
    "team",
    "purpose",
    "namespace",
    "stage",
    "environment",
    "managed-by",
    "managedby",
    "run-id",
    "runid",
    "execution-id",
    "expires-at",
    "rca:run-id",
    "rca:owner",
    "rca:purpose",
    "aws:ecs:clustername",
    "aws:ecs:servicename",
    "realisticdemorunid",
    "realisticdemojournal",
}


class EcsControlError(ValueError):
    """A fixed, safe message suitable for returning to the analyst."""


def alarm_scope(alarm: dict) -> tuple[str, str, str]:
    """Use persisted alarm scope, never an account/region supplied by the model."""
    if not alarm.get("AlarmArn") and _region_partition(alarm.get("Region")) is None:
        raise EcsControlError("missing session alarm region")
    region = parse_alarm(alarm).region
    arn = alarm.get("AlarmArn")
    account = alarm.get("AWSAccountId")
    if arn:
        if _alarm_arn_region(arn) is None:
            raise EcsControlError("invalid session alarm ARN")
        arn_account = arn.split(":", 5)[4]
        if account and account != arn_account:
            raise EcsControlError("session alarm account mismatch")
        account = arn_account
    if not isinstance(account, str) or re.fullmatch(r"[0-9]{12}", account) is None:
        raise EcsControlError("missing session alarm account")
    return _region_partition(region), region, account


def _resource(arn: str, kind: str, scope: tuple[str, str, str]) -> str:
    parts = arn.split(":", 5) if isinstance(arn, str) else []
    partition, region, account = scope
    if (
        len(parts) != 6
        or parts[:5] != ["arn", partition, "ecs", region, account]
        or _RESOURCES[kind].fullmatch(parts[5]) is None
    ):
        raise EcsControlError(f"invalid or out-of-scope ECS {kind} ARN")
    return parts[5]


def validate_target(task_arn: str, cluster_arn: str, scope: tuple[str, str, str]) -> None:
    cluster = _resource(cluster_arn, "cluster", scope).split("/")[1]
    task = _resource(task_arn, "task", scope).split("/")
    if len(task) == 3 and task[1] != cluster:
        raise EcsControlError("task ARN cluster mismatch")


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > 512 or any(ord(c) < 32 for c in value) or _SENSITIVE.search(value):
        return "[REDACTED]"
    return value


def _tags(data: dict) -> list[dict] | None:
    # Missing != empty: do not interpret absent tag data as an ownership result.
    if "tags" not in data:
        return None
    return [
        {
            "key": _text(tag.get("key")),
            "value": (
                _text(tag.get("value"))
                if isinstance(tag.get("key"), str)
                and not _SENSITIVE.search(tag["key"])
                and tag["key"].lower() in _TAG_KEYS
                else "[REDACTED]"
            ),
        }
        for tag in data["tags"]
    ]


def _times(data: dict, names: tuple[str, ...]) -> dict:
    return {
        name: value.astimezone(UTC).isoformat()
        for name in names
        if isinstance(value := data.get(name), datetime) and value.tzinfo is not None
    }


def inspect_task(ecs, task_arn: str, cluster_arn: str, scope: tuple[str, str, str]) -> dict:
    """One DescribeTasks operation; definition family/revision are ARN-derived only."""
    validate_target(task_arn, cluster_arn, scope)
    if ecs.meta.region_name != scope[1]:
        raise EcsControlError("ECS client region mismatch")
    observed_at = datetime.now(UTC).isoformat()
    try:
        response = ecs.describe_tasks(cluster=cluster_arn, tasks=[task_arn], include=["TAGS"])
    except (ClientError, BotoCoreError) as exc:
        raise EcsControlError("DescribeTasks unavailable; control ownership remains unverified") from exc
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise EcsControlError("DescribeTasks missing or failed; control ownership remains unverified")
    task = response["tasks"][0]
    if task.get("taskArn") != task_arn or task.get("clusterArn") != cluster_arn:
        raise EcsControlError("DescribeTasks identity mismatch")
    definition_arn = task.get("taskDefinitionArn")
    definition_resource = _resource(definition_arn, "task-definition", scope)
    family, revision = definition_resource.removeprefix("task-definition/").split(":")
    group = task.get("group", "")
    kind = "unknown"
    if isinstance(group, str) and group.startswith("service:") and len(group) > len("service:"):
        kind = "service"
    elif group == f"family:{family}":
        kind = "standalone"
    result = {
        "observed_at": observed_at,
        "task": {
            "task_arn": task_arn,
            "cluster_arn": cluster_arn,
            "task_definition_arn": definition_arn,
            "task_definition_arn_derived": {"family": family, "revision": int(revision)},
            "group": _text(group),
            "launch_kind": kind,
            "last_status": _text(task.get("lastStatus")),
            "desired_status": _text(task.get("desiredStatus")),
            "started_by": _text(task.get("startedBy")),
            "times": _times(task, ("createdAt", "startedAt", "stoppingAt", "stoppedAt", "executionStoppedAt")),
            "tags": _tags(task),
            "containers": [
                {
                    "name": _text(c.get("name")),
                    "last_status": _text(c.get("lastStatus")),
                    "image": _text(c.get("image")),
                    "image_digest": _text(c.get("imageDigest")),
                }
                for c in task.get("containers", [])
            ],
        },
        "limitations": [
            "Read-time metadata, not alarm-window state or proof of DB ownership/rollback.",
            "Task definition not queried; family/revision are parsed only from taskDefinitionArn.",
            "Custom task groups remain unknown; no service name or task ID is inferred.",
            "Environment, secrets, overrides, command/entrypoint and unknown tag values are omitted/redacted.",
        ],
    }
    return result
