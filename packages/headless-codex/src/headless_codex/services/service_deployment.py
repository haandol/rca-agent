"""Guard a fixed rollback and observe actual service convergence through audited reads.

An API acknowledgement is not recovery. The deployment ID returned by the actual
write, every service task, and the named application container must agree before
we record the single timestamp that can anchor recovery metrics.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable

from headless_codex.services.post_action_metrics import ObservationBudget, utc
from headless_codex.services.runbook_contract import SCOPE_FIELDS, SERVICE_SETTING_DEFAULTS


def same(left: object, right: object) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def service_settings(service: dict) -> dict:
    """Normalize absent capacity strategy only, preserving all mutable deployment settings for comparison."""
    settings = {key: service.get(key, default) for key, default in SERVICE_SETTING_DEFAULTS.items()}
    if settings["capacityProviderStrategy"] is None:
        settings["capacityProviderStrategy"] = []
    return settings


def validate_rollback_context(playbook: dict, guard: dict, wait: dict) -> None:
    """Match the reader-owned approval context, without following an S3 reference here.

    The analysis reader verifies original observations and attaches this context;
    the execution worker consumes only its approved immutable snapshot. Model
    targets and preconditions must agree with that snapshot before any CLI reads.
    """
    import re

    context = playbook.get("rollback_context")
    if not isinstance(context, dict) or set(context) - {"write_accounting"} != {
        "baseline_ref",
        "scope",
        "normal",
        "current",
        "service_settings",
    }:
        raise ValueError("reader-owned rollback_context missing from approved snapshot")
    if "write_accounting" in context:
        validate_write_accounting(context)
    ref = context["baseline_ref"]
    if (
        not isinstance(ref, dict)
        or set(ref) != {"bucket", "key", "sha256"}
        or not all(isinstance(v, str) and v.strip() for v in ref.values())
    ):
        raise ValueError("rollback baseline reference is incomplete")
    if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", ref["sha256"]):
        raise ValueError("rollback baseline reference requires SHA-256")
    scope = context["scope"]
    scope_keys = {
        "account_id",
        "region",
        "cluster_arn",
        "service_arn",
        "service_name",
        "container_name",
        "desired_count",
        "log_group",
    }
    if not isinstance(scope, dict) or set(scope) != scope_keys:
        raise ValueError("rollback context requires complete scope")
    mapped = {**scope, "cluster": scope["cluster_arn"], "service": scope["service_arn"]}
    if any(not same(mapped.get(k), guard[k]) for k in SCOPE_FIELDS):
        raise ValueError("rollback context scope differs from approved deployment")
    if (
        scope["service_name"] != guard["service"].rsplit("/", 1)[-1]
        or not isinstance(scope["log_group"], str)
        or not scope["log_group"].strip()
    ):
        raise ValueError("rollback service name/log group unavailable or mismatched")
    if not same(context["service_settings"], guard["service_settings"]):
        raise ValueError("rollback context settings differ from approval")
    normal, current = context["normal"], context["current"]
    if not isinstance(normal, dict) or set(normal) != {"task_definition_arn", "image_digest"}:
        raise ValueError("rollback normal context requires definition and digest")
    if not isinstance(current, dict) or set(current) != {"task_definition_arn", "image_digest", "deployment_id"}:
        raise ValueError("rollback current context requires definition, digest and deployment identity")
    if normal["task_definition_arn"] != wait["task_definition"] or normal["image_digest"] != wait["image_digest"]:
        raise ValueError("model rollback target differs from reader-verified normal context")
    if (
        current["task_definition_arn"] != guard["expected_task_definition"]
        or current["image_digest"] != guard["expected_image_digest"]
        or current["deployment_id"] != guard["expected_deployment_id"]
    ):
        raise ValueError("model precondition differs from reader-verified current context")


def validate_write_accounting(context: dict) -> dict:
    """Require the optional server descriptor to name the approved normal image and log scope."""
    import re

    descriptor = context.get("write_accounting")
    keys = {
        "namespace",
        "dimensions",
        "attempts_metric",
        "failures_metric",
        "operation_kind",
        "accounting",
        "source_ref",
        "task_definition_arn",
        "image_digest",
    }
    if not isinstance(descriptor, dict) or set(descriptor) != keys:
        raise ValueError("invalid normal write_accounting descriptor shape")
    scope, normal = context.get("scope", {}), context.get("normal", {})
    dimensions = descriptor["dimensions"]
    if (
        descriptor["operation_kind"] != "write"
        or descriptor["accounting"] != "completed"
        or not isinstance(dimensions, dict)
        or set(dimensions) != {"ServiceName"}
        or not isinstance(dimensions["ServiceName"], str)
        or not dimensions["ServiceName"].strip()
        or any(not isinstance(descriptor[k], str) or not descriptor[k].strip() for k in keys - {"dimensions"})
        or any(descriptor[k] != normal.get(k) for k in ("task_definition_arn", "image_digest"))
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", descriptor["image_digest"])
    ):
        raise ValueError("write_accounting differs from reader-verified normal context")
    prefix = "cloudwatch-logs://" + str(scope.get("log_group", "")) + "/"
    source = descriptor["source_ref"]
    stream, separator, event_id = source.removeprefix(prefix).partition("#")
    parts = stream.split("/")
    if (
        not source.startswith(prefix)
        or not separator
        or not event_id
        or len(parts) < 3
        or parts[-2] != scope.get("container_name")
        or not parts[-1]
    ):
        raise ValueError("write_accounting source is outside approved normal log scope")
    return descriptor


def command(operation: str, scope: dict, *args: str) -> str:
    return shlex.join(["aws", "ecs", operation, *args, "--region", scope["region"], "--output", "json"])


def read_json(command_text: str, budget: ObservationBudget, run: Callable) -> dict:
    budget.remaining()
    result = run(command_text, budget)
    budget.remaining()
    if not result.get("ok") or result.get("output_incomplete") or result.get("stdout_truncated"):
        raise ValueError("ECS observation unavailable or incomplete")
    payload = json.loads(result["stdout"])
    if not isinstance(payload, dict) or payload.get("failures") or payload.get("nextToken"):
        raise ValueError("ECS response failed or pagination incomplete")
    return payload


def read_service(scope: dict, budget: ObservationBudget, run: Callable) -> dict:
    payload = read_json(
        command("describe-services", scope, "--cluster", scope["cluster"], "--services", scope["service"]), budget, run
    )
    services = payload.get("services", [])
    if len(services) != 1:
        raise ValueError("exactly one approved ECS service required")
    service = services[0]
    if service.get("serviceArn") != scope["service"] or service.get("clusterArn") != scope["cluster"]:
        raise ValueError("ECS service response scope mismatch")
    if service.get("status") != "ACTIVE" or service.get("desiredCount") != scope["desired_count"]:
        raise ValueError("service status or desired count drift")
    if service.get("deploymentController", {"type": "ECS"}).get("type") != "ECS":
        raise ValueError("only ECS rolling service deployments supported")
    return service


def read_tasks(scope: dict, budget: ObservationBudget, run: Callable) -> list[dict]:
    arns = []
    # PENDING tasks normally have desiredStatus RUNNING. Query both values to fail
    # closed if an API/provider surfaces a separate pending task population.
    for status in ("RUNNING", "PENDING"):
        result = read_json(
            command(
                "list-tasks",
                scope,
                "--cluster",
                scope["cluster"],
                "--service-name",
                scope["service"].rsplit("/", 1)[-1],
                "--desired-status",
                status,
            ),
            budget,
            run,
        )
        batch = result.get("taskArns")
        if not isinstance(batch, list) or len(set(batch)) != len(batch):
            raise ValueError("invalid/duplicate service task identities")
        arns.extend(batch)
    arns = list(dict.fromkeys(arns))
    tasks = []
    for start in range(0, len(arns), 100):
        batch = arns[start : start + 100]
        response = read_json(
            command("describe-tasks", scope, "--cluster", scope["cluster"], "--tasks", *batch), budget, run
        )
        rows = response.get("tasks", [])
        if len(rows) != len(batch) or {t.get("taskArn") for t in rows} != set(batch):
            raise ValueError("incomplete/duplicate task description")
        tasks.extend(rows)
    for task in tasks:
        if (
            task.get("clusterArn") != scope["cluster"]
            or task.get("group") != "service:" + scope["service"].rsplit("/", 1)[-1]
        ):
            raise ValueError("task does not belong to approved service")
    return tasks


def task_matches(task: dict, scope: dict, definition: str, digest: str) -> bool:
    containers = task.get("containers", [])
    names = [c.get("name") for c in containers]
    if len(set(names)) != len(names):
        raise ValueError("duplicate container identity")
    app = [c for c in containers if c.get("name") == scope["container_name"]]
    return (
        task.get("taskDefinitionArn") == definition
        and task.get("lastStatus") == "RUNNING"
        and task.get("desiredStatus") == "RUNNING"
        and task.get("healthStatus") == "HEALTHY"
        and len(app) == 1
        and app[0].get("lastStatus") == "RUNNING"
        and app[0].get("healthStatus") == "HEALTHY"
        and app[0].get("imageDigest") == digest
    )


def verify_target(wait: dict, budget: ObservationBudget, run: Callable) -> None:
    data = read_json(
        command("describe-task-definition", wait, "--task-definition", wait["task_definition"]), budget, run
    )
    definition = data.get("taskDefinition", {})
    apps = [c for c in definition.get("containerDefinitions", []) if c.get("name") == wait["container_name"]]
    if definition.get("taskDefinitionArn") != wait["task_definition"] or definition.get("status") != "ACTIVE":
        raise ValueError("approved healthy task definition unavailable/inactive")
    if len(apps) != 1 or not apps[0].get("image", "").endswith("@" + wait["image_digest"]):
        raise ValueError("target app image must reference the approved immutable digest")


def check_precondition(guard: dict, wait: dict, budget: ObservationBudget, run: Callable) -> dict:
    verify_target(wait, budget, run)
    tasks = read_tasks(guard, budget, run)
    # Last read before the write checks the service again after task/definition reads.
    service = read_service(guard, budget, run)
    if not same(service_settings(service), guard["service_settings"]):
        raise ValueError("service configuration drift; new approval required")
    deployments = service.get("deployments", [])
    if service.get("taskDefinition") != guard["expected_task_definition"]:
        raise ValueError("current service is not the approved fault; already healthy is not this action")
    if (
        len(deployments) != 1
        or deployments[0].get("taskDefinition") != guard["expected_task_definition"]
        or deployments[0].get("status") != "PRIMARY"
        or deployments[0].get("rolloutState") != "COMPLETED"
        or deployments[0].get("id") != guard["expected_deployment_id"]
    ):
        raise ValueError("foreign or incomplete deployment before rollback")
    if (
        service.get("pendingCount") != 0
        or service.get("runningCount") != guard["desired_count"]
        or len(tasks) != guard["desired_count"]
    ):
        raise ValueError("fault service task population changed")
    if not all(
        task_matches(t, guard, guard["expected_task_definition"], guard["expected_image_digest"]) for t in tasks
    ):
        raise ValueError("fault task definition/image/health changed")
    return {
        "fault_deployment_id": deployments[0]["id"],
        "task_arns": [t["taskArn"] for t in tasks],
        "service_settings": service_settings(service),
    }


def verify_update_response(payload: dict, guard: dict, wait: dict, precondition: dict) -> str:
    service = payload.get("service", {})
    if any(
        service.get(k) != v
        for k, v in (
            ("serviceArn", wait["service"]),
            ("clusterArn", wait["cluster"]),
            ("taskDefinition", wait["task_definition"]),
            ("desiredCount", wait["desired_count"]),
        )
    ):
        raise ValueError("UpdateService response scope differs from approved rollback")
    if not same(service_settings(service), guard["service_settings"]):
        raise ValueError("UpdateService response settings drift")
    deployments = service.get("deployments", [])
    primary = [d for d in deployments if d.get("status") == "PRIMARY"]
    if (
        len(primary) != 1
        or primary[0].get("taskDefinition") != wait["task_definition"]
        or not primary[0].get("id")
        or primary[0]["id"] == precondition["fault_deployment_id"]
    ):
        raise ValueError("UpdateService response has no new approved deployment")
    if any(d.get("id") not in {primary[0]["id"], precondition["fault_deployment_id"]} for d in deployments):
        raise ValueError("foreign deployment in UpdateService response")
    return primary[0]["id"]


def _check_convergence_service(service: dict, wait: dict, guard: dict, action: dict) -> list[dict]:
    """Validate each service read independently before treating a change as rollout progress."""
    if service.get("taskDefinition") != wait["task_definition"] or not same(
        service_settings(service), guard["service_settings"]
    ):
        raise ValueError("service definition/configuration drift during deployment")
    deployments = service.get("deployments", [])
    expected = {
        action["deployment_id"]: wait["task_definition"],
        action["ecs_service_precondition_receipt"]["fault_deployment_id"]: guard["expected_task_definition"],
    }
    ids = [d.get("id") for d in deployments]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate deployment identity during convergence")
    for deployment in deployments:
        target = expected.get(deployment.get("id"))
        if target is None or deployment.get("rolloutState") == "FAILED" or deployment.get("taskDefinition") != target:
            raise ValueError("foreign, failed or changed deployment during convergence")
    primary = [d for d in deployments if d.get("status") == "PRIMARY"]
    if len(primary) != 1 or primary[0].get("id") != action["deployment_id"]:
        raise ValueError("approved deployment is no longer primary")
    return deployments


def _convergence_observation(service: dict) -> dict:
    """Compare convergence-relevant values by deployment ID, ignoring history and array order."""
    fields = ("taskDefinition", "status", "rolloutState", "desiredCount", "runningCount", "pendingCount")
    return {
        "runningCount": service.get("runningCount"),
        "pendingCount": service.get("pendingCount"),
        "deployments": {d["id"]: {key: d.get(key) for key in fields} for d in service.get("deployments", [])},
    }


def poll_deployment(
    wait: dict,
    guard: dict,
    action: dict,
    budget: ObservationBudget,
    run: Callable,
    append: Callable,
    *,
    wait_for_convergence: bool = True,
) -> dict:
    binding = {"request": wait, "action_record": action["ended_at"], "deadline": utc(budget.deadline)}
    while True:
        budget.remaining()
        service = read_service(wait, budget, run)
        _check_convergence_service(service, wait, guard, action)
        tasks = read_tasks(wait, budget, run)
        # Even a changing snapshot cannot hide a task outside the two approved versions.
        if any(
            t.get("taskDefinitionArn") not in {wait["task_definition"], guard["expected_task_definition"]}
            for t in tasks
        ):
            raise ValueError("foreign task definition during convergence")
        # Recheck service after task reads so a new deployment is not hidden by the first read.
        after = read_service(wait, budget, run)
        deployments = _check_convergence_service(after, wait, guard, action)
        changed = not same(_convergence_observation(service), _convergence_observation(after))
        primary = [d for d in deployments if d.get("status") == "PRIMARY"]
        # An allowed lifecycle change needs a fresh complete round. Neither
        # progress nor removal is failure, but mixed-time reads cannot prove health.
        prior_arns = [] if changed else action["ecs_service_precondition_receipt"]["task_arns"]
        prior_tasks = []
        for start in range(0, len(prior_arns), 100):
            batch = prior_arns[start : start + 100]
            response = read_json(
                command("describe-tasks", wait, "--cluster", wait["cluster"], "--tasks", *batch), budget, run
            )
            rows = response.get("tasks", [])
            if len(rows) != len(batch) or {t.get("taskArn") for t in rows} != set(batch):
                raise ValueError("prior fault task disappearance is not a stopped-task receipt")
            if any(
                t.get("clusterArn") != wait["cluster"]
                or t.get("taskDefinitionArn") != guard["expected_task_definition"]
                for t in rows
            ):
                raise ValueError("prior fault task response scope mismatch")
            prior_tasks.extend(rows)
        healthy = (
            not changed
            and all(t.get("lastStatus") == "STOPPED" for t in prior_tasks)
            and len(deployments) == 1
            and primary[0].get("rolloutState") == "COMPLETED"
            and primary[0].get("taskDefinition") == wait["task_definition"]
            and after.get("runningCount") == wait["desired_count"]
            and after.get("pendingCount") == 0
            and primary[0].get("runningCount") == wait["desired_count"]
            and primary[0].get("pendingCount") == 0
            and len(tasks) == wait["desired_count"]
            and all(task_matches(t, wait, wait["task_definition"], wait["image_digest"]) for t in tasks)
        )
        if not healthy and not wait_for_convergence:
            raise ValueError("approved healthy deployment no longer converged during metric observation")
        budget.remaining()
        observed = utc(budget.clock())
        receipt = {
            "type": "deployment_wait",
            "phase": "terminal" if healthy else "poll",
            "status": "HEALTHY" if healthy else "PENDING",
            "ok": healthy,
            "binding": binding,
            "observed_at": observed,
            "deployment_id": action["deployment_id"],
            "task_arns": [t["taskArn"] for t in tasks],
        }
        if changed:
            receipt["observation_changed"] = True
        if healthy:
            receipt["first_converged_at"] = observed
        append(receipt)
        if healthy:
            return receipt
        budget.pause()


def approved_action(steps: list[dict], wait: dict, records: list[dict], execution_id: str) -> dict:
    """Replay the write receipt rather than trusting a narrative/standalone wait marker."""
    from headless_codex.services.execution_contract import command_records, step_contract_error

    step = next(s for s in steps if s["step_id"] == wait["action_step_id"])
    if step_contract_error(step, records):
        raise ValueError("approved guarded rollback has no complete successful attempt")
    attempts = command_records(step, records, 0)
    action = attempts[-1]
    if action.get("execution_id") != execution_id or action.get("stdout_truncated") or action.get("output_incomplete"):
        raise ValueError("same-execution intact guarded write response required")
    guard = step["ecs_service_precondition"]
    pre = action.get("ecs_service_precondition_receipt", {})
    if pre.get("fault_deployment_id") != guard["expected_deployment_id"] or not same(
        pre.get("service_settings"), guard["service_settings"]
    ):
        raise ValueError("write precondition receipt does not match approval")
    arns = pre.get("task_arns")
    prefix = f"arn:aws:ecs:{wait['region']}:{wait['account_id']}:task/"
    if (
        not isinstance(arns, list)
        or len(arns) != wait["desired_count"]
        or len(set(arns)) != len(arns)
        or not all(isinstance(a, str) and a.startswith(prefix) for a in arns)
    ):
        raise ValueError("write precondition task identities unavailable")
    deployment_id = verify_update_response(json.loads(action["stdout"]), guard, wait, pre)
    if action.get("deployment_id") != deployment_id:
        raise ValueError("write deployment identity differs from actual response")
    return action
