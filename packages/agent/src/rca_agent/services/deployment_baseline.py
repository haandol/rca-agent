"""Build approval provenance only from the application's verified observation reader."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from rca_agent.ports.dto.models import ScopingResult
from rca_agent.services.runbook_contract import (
    SCOPE_FIELDS,
    SERVICE_SETTING_DEFAULTS,
    _command_parts,
    rollback_argv,
)


def build_rollback_context(scoping: ScopingResult | None) -> dict | None:
    """Require the same service/settings and a single observed fault deployment before offering rollback."""
    if scoping is None or not scoping.incident_observations.baseline_verified:
        return None
    observations = scoping.incident_observations
    baseline, current = observations.baseline, observations.current
    try:
        scope = baseline["scope"]
        if current["scope"] != scope:
            return None
        normal_settings = {
            key: baseline["service_settings"].get(key, default) for key, default in SERVICE_SETTING_DEFAULTS.items()
        }
        fault_settings = {
            key: current["service_settings"].get(key, default) for key, default in SERVICE_SETTING_DEFAULTS.items()
        }
        # AWS may omit this optional collection while CLI snapshots encode null.
        # Normalize only that representational absence; retain every supplied
        # strategy entry and every nested mutable deployment setting verbatim.
        for settings in (normal_settings, fault_settings):
            if settings["capacityProviderStrategy"] is None:
                settings["capacityProviderStrategy"] = []
        if (
            normal_settings != fault_settings
            or scope["desired_count"] != current["service_settings"]["desiredCount"]
            or scope["desired_count"] != len(current["tasks"])
        ):
            return None
        deployments = current["deployments"]
        if len(deployments) != 1:
            return None
        deployment = deployments[0]
        if (
            deployment["taskDefinition"] != current["task_definition_arn"]
            or deployment["status"] != "PRIMARY"
            or deployment["rolloutState"] != "COMPLETED"
            or not deployment["id"]
        ):
            return None
        observed_at = baseline["observed_at"]
        if not datetime.fromisoformat(observed_at) < datetime.fromisoformat(current["observed_at"]):
            return None
        if not datetime.fromisoformat(observed_at) < datetime.fromisoformat(deployment["createdAt"]):
            return None
        if (
            scoping.raw_alarm is None
            or scoping.raw_alarm.state_change_time is None
            or datetime.fromisoformat(deployment["createdAt"]) > scoping.raw_alarm.state_change_time
        ):
            return None
        if current["deployment_id"] != deployment["id"]:
            return None
        digests = set()
        for task in current["tasks"]:
            if task["task_definition_arn"] != current["task_definition_arn"] or task["last_status"] != "RUNNING":
                return None
            containers = [c for c in task["containers"] if c["name"] == scope["container_name"]]
            if len(containers) != 1:
                return None
            digests.add(containers[0]["imageDigest"])
        if len(digests) != 1:
            return None
        fault_digest = next(iter(digests))
        if fault_digest != current["image_digest"]:
            return None
        if (
            baseline["normal"]["task_definition_arn"] == current["task_definition_arn"]
            or baseline["normal"]["image_digest"] == fault_digest
        ):
            return None
        context = deepcopy(
            {
                "baseline_ref": baseline["baseline_ref"],
                "scope": scope,
                "normal": baseline["normal"],
                "current": {
                    "task_definition_arn": current["task_definition_arn"],
                    "image_digest": fault_digest,
                    "deployment_id": deployment["id"],
                },
                "service_settings": normal_settings,
            }
        )
        if descriptor := normal_write_accounting(baseline):
            context["write_accounting"] = descriptor
        return context
    except (KeyError, ValueError, TypeError):
        return None


def normal_write_accounting(baseline: dict) -> dict | None:
    """Normalize only the known producer contract from reader-verified normal image logs."""
    metrics = baseline["metrics"]
    attempts, failures = metrics["attempts"], metrics["failures"]
    dimensions = attempts["dimensions"]
    if (
        not isinstance(dimensions, dict)
        or set(dimensions) != {"ServiceName"}
        or not isinstance(dimensions["ServiceName"], str)
        or not dimensions["ServiceName"].strip()
        or failures["dimensions"] != dimensions
        or failures["namespace"] != attempts["namespace"]
    ):
        return None
    expected = {
        "metric_namespace": attempts["namespace"],
        "service_name": dimensions["ServiceName"],
        "attempt_metric": attempts["metric_name"],
        "failure_metric": failures["metric_name"],
        "attempt_semantics": "completed_successful_rows_plus_failed_rows",
        "failure_semantics": "failed_rows",
        "cancellation_semantics": "excluded_from_completed_counters",
        "success_evidence_event": "write_completed",
        "success_count_field": "count",
        "success_semantics": "committed_rows",
    }
    events = [e for e in baseline["observations"] if e["message"].get("event") == "write_accounting"]
    if not events or any(any(e["message"].get(k) != v for k, v in expected.items()) for e in events):
        return None
    event = events[0]
    return {
        "namespace": attempts["namespace"],
        "dimensions": dimensions,
        "attempts_metric": attempts["metric_name"],
        "failures_metric": failures["metric_name"],
        "operation_kind": "write",
        "accounting": "completed",
        "source_ref": f"cloudwatch-logs://{event['log_group']}/{event['log_stream']}#{event['event_id']}",
        **baseline["normal"],
    }


def validate_observed_plan(steps: list[dict], baseline: dict, scoping: ScopingResult) -> None:
    """Reject model-authored drift in fixed targets, settings and recovery coordinates."""
    normal, fault = baseline["normal"], baseline["current"]
    scope = {
        **baseline["scope"],
        "cluster": baseline["scope"]["cluster_arn"],
        "service": baseline["scope"]["service_arn"],
    }
    expected_guard = {
        **{key: scope[key] for key in SCOPE_FIELDS},
        "expected_task_definition": fault["task_definition_arn"],
        "expected_image_digest": fault["image_digest"],
        "expected_deployment_id": fault["deployment_id"],
        "service_settings": baseline["service_settings"],
    }
    guarded = set()
    deployment_steps = set()
    recovered = set()
    for step in steps:
        commands = step.get("commands", [])
        for command in commands:
            if _command_parts(command)[0][:3] == ["aws", "ecs", "update-service"]:
                options = rollback_argv(command)
                if (
                    step.get("ecs_service_precondition") != expected_guard
                    or options["--task-definition"] != normal["task_definition_arn"]
                ):
                    raise ValueError("rollback is not bound to the observed normal/fault deployment")
                guarded.add(step["step_id"])
        if wait := step.get("deployment_wait"):
            if (
                wait["action_step_id"] not in guarded
                or any(wait[key] != scope[key] for key in SCOPE_FIELDS)
                or wait["task_definition"] != normal["task_definition_arn"]
                or wait["image_digest"] != normal["image_digest"]
            ):
                raise ValueError("deployment wait differs from observed normal deployment")
            deployment_steps.add(step["step_id"])
        if (wait := step.get("metric_wait")) and wait.get("deployment_step_id"):
            if (
                wait["deployment_step_id"] not in deployment_steps
                or scoping.raw_alarm is None
                or wait["failure_alarm_name"] != scoping.raw_alarm.alarm_name
                or wait["metrics"]
                != {key: scoping.incident_observations.baseline["metrics"][key] for key in wait["metrics"]}
            ):
                raise ValueError("recovery observation differs from the current incident")
            if baseline.get("write_accounting") and wait.get("completed_work_evidence") != {
                "record_index": "approved_context",
                "json_pointer": "/playbook/rollback_context/write_accounting",
            }:
                raise ValueError(
                    "verified normal write_accounting is available; metric_wait.completed_work_evidence must reference "
                    "/playbook/rollback_context/write_accounting in approved_context"
                )
            recovered.add(wait["deployment_step_id"])
    if guarded and (not deployment_steps or recovered != deployment_steps):
        raise ValueError("rollback requires deployment convergence and subsequent recovery observations")
