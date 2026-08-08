#!/usr/bin/env python3
"""Deploy, inspect, or clean up the Healthcare flagship fault.

Every mutating command accepts a caller-owned run id. Reusing that id across
red-herring, fault, and cleanup deployments leaves a stable lineage marker in
the ECS task definitions and CloudTrail events.

Usage:
    inject_deployment_fault.py red-herring --run-id <id>
    inject_deployment_fault.py db-leak --run-id <id>
    inject_deployment_fault.py cleanup --run-id <id>
    inject_deployment_fault.py status --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

CLUSTER = "RcaAgentDevHealthcare"
SERVICE = "RcaAgentDevHealthcare"
FAMILY = "RcaAgentDevHealthcare"
CONTAINER = "healthcare"
DB_INSTANCE = "rcaagentdev-postgres"
ALARM_NAMES = (
    "RcaAgentDev-Healthcare-RdsHighConnections",
    "RcaAgentDev-Healthcare-VitalIngestFailures",
)

FAULT_FLAGS = {
    "db-leak": {"FAULT_DB_LEAK": "true"},
    "slow-query": {"FAULT_SLOW_QUERY_MS": "1500"},
    "error-rate": {"FAULT_ERROR_RATE": "0.25"},
}

CLEARED_FLAGS = {
    "FAULT_DB_LEAK": "false",
    "FAULT_SLOW_QUERY_MS": "0",
    "FAULT_ERROR_RATE": "0.0",
}

# This value never changes application behavior. The run id makes every
# red-herring revision distinct even when LOG_LEVEL was already DEBUG.
RED_HERRING_FLAGS = {"LOG_LEVEL": "DEBUG"}
MANAGED_ENVIRONMENT_KEYS = (
    *CLEARED_FLAGS,
    "LOG_LEVEL",
    "DEPLOYED_REVISION",
    "RCA_TEST_RUN_ID",
    "RCA_TEST_PHASE",
)
PARAMETER_GROUP_RUN_TAG = "RCA_TEST_RUN_ID"
PARAMETER_GROUP_PROVENANCE_TAG = "RCA_TEST_PROVENANCE"
PARAMETER_GROUP_PROVENANCE = "inject_deployment_fault.py:db-leak"

_aws_context: list[str] = []
RECOVERABLE_ERRORS = (
    OSError,
    KeyError,
    RuntimeError,
    TimeoutError,
    TypeError,
    ValueError,
)


class AwsCommandError(RuntimeError):
    """An AWS CLI command failed."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def aws(*args: str) -> dict[str, Any]:
    result = subprocess.run(
        ["aws", *_aws_context, *args, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f"exit code {result.returncode}"
        raise AwsCommandError(f"aws {' '.join(args)} failed: {message}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def describe_service() -> dict[str, Any]:
    response = aws(
        "ecs",
        "describe-services",
        "--cluster",
        CLUSTER,
        "--services",
        SERVICE,
    )
    services = response.get("services") or []
    if len(services) != 1 or not services[0].get("taskDefinition"):
        raise RuntimeError(f"ECS service {CLUSTER}/{SERVICE} was not found")
    return services[0]


def service_task_definition_arn() -> str:
    return str(describe_service()["taskDefinition"])


def current_task_definition() -> dict[str, Any]:
    return aws(
        "ecs",
        "describe-task-definition",
        "--task-definition",
        service_task_definition_arn(),
    )["taskDefinition"]


def container_environment(task_definition: dict[str, Any]) -> dict[str, str]:
    for container in task_definition["containerDefinitions"]:
        if container["name"] == CONTAINER:
            return {
                entry["name"]: entry["value"]
                for entry in container.get("environment", [])
            }
    raise RuntimeError(f"container {CONTAINER!r} not found in task definition {FAMILY}")


def register_with_environment(
    task_definition: dict[str, Any],
    overrides: dict[str, str],
    *,
    removals: set[str] | None = None,
) -> str:
    found = False
    for container in task_definition["containerDefinitions"]:
        if container["name"] != CONTAINER:
            continue
        found = True
        environment = {
            entry["name"]: entry["value"] for entry in container.get("environment", [])
        }
        for key in removals or set():
            environment.pop(key, None)
        environment.update(overrides)
        container["environment"] = [
            {"name": key, "value": value} for key, value in sorted(environment.items())
        ]
    if not found:
        raise RuntimeError(
            f"container {CONTAINER!r} not found in task definition {FAMILY}"
        )

    for key in (
        "taskDefinitionArn",
        "revision",
        "status",
        "requiresAttributes",
        "compatibilities",
        "registeredAt",
        "registeredBy",
        "deregisteredAt",
    ):
        task_definition.pop(key, None)

    registered = aws(
        "ecs",
        "register-task-definition",
        "--cli-input-json",
        json.dumps(task_definition),
    )
    return str(registered["taskDefinition"]["taskDefinitionArn"])


def deploy(task_definition_arn: str, *, wait: bool) -> None:
    aws(
        "ecs",
        "update-service",
        "--cluster",
        CLUSTER,
        "--service",
        SERVICE,
        "--task-definition",
        task_definition_arn,
    )
    if wait:
        aws(
            "ecs",
            "wait",
            "services-stable",
            "--cluster",
            CLUSTER,
            "--services",
            SERVICE,
        )
        actual = service_task_definition_arn()
        if actual != task_definition_arn:
            raise RuntimeError(
                "ECS service stabilized on an unexpected task definition: "
                f"{actual} != {task_definition_arn}"
            )


def apply(
    overrides: dict[str, str],
    label: str,
    *,
    run_id: str,
    wait: bool,
    removals: set[str] | None = None,
    mark_active: bool = True,
) -> dict[str, Any]:
    started_at = utc_now()
    deployment_overrides = dict(overrides)
    deployment_removals = set(removals or set())
    if mark_active:
        deployment_overrides.update(
            {
                "RCA_TEST_RUN_ID": run_id,
                "RCA_TEST_PHASE": label,
            }
        )
    else:
        deployment_removals.update({"RCA_TEST_RUN_ID", "RCA_TEST_PHASE"})
    arn = register_with_environment(
        current_task_definition(),
        deployment_overrides,
        removals=deployment_removals,
    )
    deploy(arn, wait=wait)
    return {
        "action": label,
        "runId": run_id,
        "startedAt": started_at,
        "completedAt": utc_now(),
        "taskDefinitionArn": arn,
        "serviceStable": wait,
        "overrides": deployment_overrides,
        "removedEnvironment": sorted(deployment_removals),
        "ownedResources": {"dbParameterGroups": []},
    }


def alarm_states() -> dict[str, str]:
    response = aws(
        "cloudwatch",
        "describe-alarms",
        "--alarm-names",
        *ALARM_NAMES,
    )
    return {
        str(alarm["AlarmName"]): str(alarm["StateValue"])
        for alarm in response.get("MetricAlarms", [])
    }


def wait_for_alarms_ok(*, timeout_seconds: int) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, str] = {}
    while time.monotonic() < deadline:
        latest = alarm_states()
        if all(latest.get(name) == "OK" for name in ALARM_NAMES):
            return latest
        time.sleep(10)
    raise TimeoutError(
        f"alarms did not return to OK within {timeout_seconds}s: "
        f"{json.dumps(latest, sort_keys=True)}"
    )


def describe_db_instance() -> dict[str, Any]:
    response = aws(
        "rds",
        "describe-db-instances",
        "--db-instance-identifier",
        DB_INSTANCE,
    )
    instances = response.get("DBInstances") or []
    if len(instances) != 1:
        raise RuntimeError(f"RDS instance {DB_INSTANCE} was not found")
    return instances[0]


def current_parameter_group(instance: dict[str, Any]) -> str:
    groups = instance.get("DBParameterGroups") or []
    if not groups or not groups[0].get("DBParameterGroupName"):
        raise RuntimeError(f"RDS instance {DB_INSTANCE} has no parameter group")
    return str(groups[0]["DBParameterGroupName"])


def current_parameter_apply_status(instance: dict[str, Any]) -> str | None:
    groups = instance.get("DBParameterGroups") or []
    if not groups:
        return None
    status = groups[0].get("ParameterApplyStatus")
    return str(status) if status is not None else None


def parameter_group_names() -> list[str]:
    response = aws("rds", "describe-db-parameter-groups")
    return sorted(
        str(group["DBParameterGroupName"])
        for group in response.get("DBParameterGroups", [])
        if group.get("DBParameterGroupName")
    )


def restore_parameter_group(
    group_name: str,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    instance = describe_db_instance()
    current = current_parameter_group(instance)
    changed = current != group_name
    if changed:
        aws(
            "rds",
            "modify-db-instance",
            "--db-instance-identifier",
            DB_INSTANCE,
            "--db-parameter-group-name",
            group_name,
            "--apply-immediately",
        )
        aws(
            "rds",
            "wait",
            "db-instance-available",
            "--db-instance-identifier",
            DB_INSTANCE,
        )

    deadline = time.monotonic() + timeout_seconds
    rebooted = False
    latest_group = current
    latest_status = current_parameter_apply_status(instance)
    while time.monotonic() < deadline:
        refreshed = describe_db_instance()
        latest_group = current_parameter_group(refreshed)
        latest_status = current_parameter_apply_status(refreshed)
        if latest_group == group_name and latest_status == "in-sync":
            return {
                "previousParameterGroup": current,
                "restoredParameterGroup": group_name,
                "parameterApplyStatus": latest_status,
                "changed": changed,
                "rebooted": rebooted,
            }
        if (
            latest_group == group_name
            and latest_status == "pending-reboot"
            and not rebooted
        ):
            aws(
                "rds",
                "reboot-db-instance",
                "--db-instance-identifier",
                DB_INSTANCE,
            )
            aws(
                "rds",
                "wait",
                "db-instance-available",
                "--db-instance-identifier",
                DB_INSTANCE,
            )
            rebooted = True
            continue
        time.sleep(10)
    raise TimeoutError(
        "DB parameter group did not become in-sync within "
        f"{timeout_seconds}s: group={latest_group!r}, status={latest_status!r}"
    )


def parameter_group_name_prefix(run_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")[:32]
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"rca-e2e-{slug or 'run'}-{digest}-"


def validate_parameter_group_ownership(
    proof: dict[str, Any],
    *,
    run_id: str,
) -> str:
    if not isinstance(proof, dict):
        raise RuntimeError("DB parameter group ownership proof must be an object")
    name = proof.get("name")
    tags = proof.get("tags")
    if not isinstance(name, str) or not name.startswith(
        parameter_group_name_prefix(run_id)
    ):
        raise RuntimeError(
            "DB parameter group ownership proof has an invalid run-scoped name"
        )
    if proof.get("runId") != run_id:
        raise RuntimeError("DB parameter group ownership proof runId mismatch")
    if proof.get("provenance") != PARAMETER_GROUP_PROVENANCE:
        raise RuntimeError("DB parameter group ownership proof provenance mismatch")
    if not isinstance(tags, dict):
        raise RuntimeError("DB parameter group ownership proof tags are missing")
    expected_tags = {
        PARAMETER_GROUP_RUN_TAG: run_id,
        PARAMETER_GROUP_PROVENANCE_TAG: PARAMETER_GROUP_PROVENANCE,
    }
    if any(tags.get(key) != value for key, value in expected_tags.items()):
        raise RuntimeError("DB parameter group ownership proof tags mismatch")
    return name


def delete_parameter_group(
    proof: dict[str, Any],
    *,
    run_id: str,
    restored_group: str | None,
) -> None:
    candidate = validate_parameter_group_ownership(proof, run_id=run_id)
    if candidate == restored_group:
        raise RuntimeError("refusing to delete the restored DB parameter group")
    if candidate.startswith("default."):
        raise RuntimeError(
            f"refusing to delete managed DB parameter group {candidate!r}"
        )
    if current_parameter_group(describe_db_instance()) == candidate:
        raise RuntimeError(
            f"refusing to delete attached DB parameter group {candidate!r}"
        )
    response = aws(
        "rds",
        "describe-db-parameter-groups",
        "--db-parameter-group-name",
        candidate,
    )
    groups = response.get("DBParameterGroups") or []
    if len(groups) != 1 or not groups[0].get("DBParameterGroupArn"):
        raise RuntimeError(
            f"cannot verify DB parameter group ownership for {candidate!r}"
        )
    tag_response = aws(
        "rds",
        "list-tags-for-resource",
        "--resource-name",
        str(groups[0]["DBParameterGroupArn"]),
    )
    actual_tags = {
        str(tag["Key"]): str(tag["Value"])
        for tag in tag_response.get("TagList", [])
        if tag.get("Key") is not None and tag.get("Value") is not None
    }
    expected_tags = proof["tags"]
    if any(actual_tags.get(key) != value for key, value in expected_tags.items()):
        raise RuntimeError(
            f"live DB parameter group tags do not prove ownership of {candidate!r}"
        )
    aws(
        "rds",
        "delete-db-parameter-group",
        "--db-parameter-group-name",
        candidate,
    )


def faults_are_clear(environment: dict[str, str]) -> bool:
    cleared_values = {
        "FAULT_DB_LEAK": {"", "0", "false"},
        "FAULT_SLOW_QUERY_MS": {"", "0", "0.0"},
        "FAULT_ERROR_RATE": {"", "0", "0.0"},
    }
    return all(
        key not in environment or environment[key].strip().lower() in allowed_values
        for key, allowed_values in cleared_values.items()
    )


def service_is_stable(service: dict[str, Any]) -> bool:
    primary = next(
        (
            deployment
            for deployment in service.get("deployments", [])
            if deployment.get("status") == "PRIMARY"
        ),
        None,
    )
    return bool(
        service.get("desiredCount") == service.get("runningCount")
        and service.get("pendingCount") == 0
        and primary
        and primary.get("rolloutState") == "COMPLETED"
        and primary.get("taskDefinition") == service.get("taskDefinition")
    )


def status_snapshot() -> dict[str, Any]:
    service = describe_service()
    task_definition = aws(
        "ecs",
        "describe-task-definition",
        "--task-definition",
        str(service["taskDefinition"]),
    )["taskDefinition"]
    environment = container_environment(task_definition)
    instance = describe_db_instance()
    alarms = alarm_states()
    flags = {
        key: value
        for key, value in environment.items()
        if key.startswith("FAULT_")
        or key
        in {
            "DEPLOYED_REVISION",
            "LOG_LEVEL",
            "RCA_TEST_RUN_ID",
        }
    }
    managed_environment = {
        key: {
            "present": key in environment,
            "value": environment.get(key),
        }
        for key in MANAGED_ENVIRONMENT_KEYS
    }
    active_run_id = (
        environment.get("RCA_TEST_RUN_ID")
        if environment.get("RCA_TEST_PHASE")
        else None
    )
    parameter_apply_status = current_parameter_apply_status(instance)
    checks = {
        "faultFlagsClear": faults_are_clear(environment),
        "serviceStable": service_is_stable(service),
        "databaseAvailable": instance.get("DBInstanceStatus") == "available",
        "databaseParameterGroupInSync": parameter_apply_status == "in-sync",
        "alarmsOk": all(alarms.get(name) == "OK" for name in ALARM_NAMES),
        "noActiveRun": active_run_id is None,
    }
    return {
        "action": "status",
        "taskDefinitionArn": task_definition["taskDefinitionArn"],
        "flags": flags,
        "environment": managed_environment,
        "logLevel": managed_environment["LOG_LEVEL"],
        "service": {
            "desiredCount": service.get("desiredCount"),
            "runningCount": service.get("runningCount"),
            "pendingCount": service.get("pendingCount"),
            "stable": checks["serviceStable"],
        },
        "database": {
            "instanceStatus": instance.get("DBInstanceStatus"),
            "parameterGroup": current_parameter_group(instance),
            "parameterApplyStatus": parameter_apply_status,
            "parameterGroups": parameter_group_names(),
        },
        "activeRunId": active_run_id,
        "alarmStates": alarms,
        "checks": checks,
        "clean": all(checks.values()),
    }


def record_cleanup_error(
    errors: list[dict[str, str]],
    step: str,
    error: Exception,
) -> None:
    errors.append(
        {
            "step": step,
            "type": type(error).__name__,
            "message": str(error),
        }
    )


def cleanup(
    *,
    run_id: str,
    wait: bool,
    timeout_seconds: int,
    restore_db_parameter_group_name: str | None,
    delete_db_parameter_groups: list[dict[str, Any]],
    restore_log_level: str | None,
    remove_log_level: bool,
) -> tuple[dict[str, Any], int]:
    started_at = utc_now()
    result: dict[str, Any] = {
        "action": "cleanup",
        "runId": run_id,
        "startedAt": started_at,
    }
    errors: list[dict[str, str]] = []

    log_overrides = (
        {"LOG_LEVEL": restore_log_level} if restore_log_level is not None else {}
    )
    log_removals = {"LOG_LEVEL"} if remove_log_level else set()
    try:
        result["ecs"] = apply(
            {**CLEARED_FLAGS, **log_overrides},
            "cleanup",
            run_id=run_id,
            wait=wait,
            removals=log_removals,
            mark_active=False,
        )
    except RECOVERABLE_ERRORS as error:
        record_cleanup_error(errors, "ecs", error)

    if restore_db_parameter_group_name:
        try:
            result["database"] = restore_parameter_group(
                restore_db_parameter_group_name,
                timeout_seconds=timeout_seconds,
            )
        except RECOVERABLE_ERRORS as error:
            record_cleanup_error(errors, "database.restore", error)

    deleted: list[str] = []
    for proof in delete_db_parameter_groups:
        candidate = str(proof.get("name", "<invalid-proof>"))
        try:
            delete_parameter_group(
                proof,
                run_id=run_id,
                restored_group=restore_db_parameter_group_name,
            )
            deleted.append(candidate)
        except RECOVERABLE_ERRORS as error:
            record_cleanup_error(
                errors,
                f"database.delete:{candidate}",
                error,
            )
    result["deletedParameterGroups"] = deleted

    try:
        result["alarmStates"] = wait_for_alarms_ok(
            timeout_seconds=timeout_seconds,
        )
    except RECOVERABLE_ERRORS as error:
        record_cleanup_error(errors, "alarms", error)

    final_status: dict[str, Any] | None = None
    try:
        final_status = status_snapshot()
        result["finalStatus"] = final_status
    except RECOVERABLE_ERRORS as error:
        record_cleanup_error(errors, "verify", error)

    checks: dict[str, bool] = {}
    if final_status:
        checks.update(final_status["checks"])
        if restore_db_parameter_group_name:
            checks["databaseParameterGroupRestored"] = (
                final_status["database"]["parameterGroup"]
                == restore_db_parameter_group_name
            )
        log_level = final_status["environment"]["LOG_LEVEL"]
        checks["logLevelRestored"] = (
            log_level["present"] and log_level["value"] == restore_log_level
            if restore_log_level is not None
            else not log_level["present"]
        )

    result["checks"] = checks
    result["errors"] = errors
    result["completedAt"] = utc_now()
    result["clean"] = bool(checks) and all(checks.values()) and not errors
    return result, 0 if result["clean"] else 1


def parse_parameter_group_proofs(
    values: list[str],
    parser: argparse.ArgumentParser,
) -> list[dict[str, Any]]:
    proofs: list[dict[str, Any]] = []
    for value in values:
        try:
            proof = json.loads(value)
        except json.JSONDecodeError as error:
            parser.error(
                "--delete-db-parameter-group must be a JSON ownership proof: "
                f"{error.msg}"
            )
        if not isinstance(proof, dict):
            parser.error(
                "--delete-db-parameter-group must be a JSON ownership proof object"
            )
        proofs.append(proof)
    return proofs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[*FAULT_FLAGS, "reset", "red-herring", "cleanup", "status"],
    )
    parser.add_argument("--run-id")
    parser.add_argument("--profile")
    parser.add_argument("--region")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--restore-db-parameter-group")
    parser.add_argument(
        "--delete-db-parameter-group",
        action="append",
        default=[],
    )
    log_level_group = parser.add_mutually_exclusive_group()
    log_level_group.add_argument("--restore-log-level")
    log_level_group.add_argument("--remove-log-level", action="store_true")
    args = parser.parse_args()
    if args.action != "status" and not args.run_id:
        parser.error("--run-id is required for mutating actions")
    if args.run_id is not None:
        args.run_id = args.run_id.strip()
        if not args.run_id:
            parser.error("--run-id must not be blank")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    cleanup_options = (
        args.restore_db_parameter_group
        or args.delete_db_parameter_group
        or args.restore_log_level is not None
        or args.remove_log_level
    )
    if args.action != "cleanup" and cleanup_options:
        parser.error("restore options are valid only with cleanup")
    if args.delete_db_parameter_group and not args.restore_db_parameter_group:
        parser.error(
            "--delete-db-parameter-group requires --restore-db-parameter-group"
        )
    args.delete_db_parameter_group = parse_parameter_group_proofs(
        args.delete_db_parameter_group,
        parser,
    )
    return args


def run() -> int:
    args = parse_args()
    if args.profile:
        _aws_context.extend(["--profile", args.profile])
    if args.region:
        _aws_context.extend(["--region", args.region])

    if args.action == "status":
        result = status_snapshot()
        exit_code = 0
    elif args.action == "cleanup":
        result, exit_code = cleanup(
            run_id=args.run_id,
            wait=not args.no_wait,
            timeout_seconds=args.timeout_seconds,
            restore_db_parameter_group_name=args.restore_db_parameter_group,
            delete_db_parameter_groups=args.delete_db_parameter_group,
            restore_log_level=args.restore_log_level,
            remove_log_level=(args.remove_log_level or args.restore_log_level is None),
        )
    elif args.action == "reset":
        result = apply(
            CLEARED_FLAGS,
            "reset",
            run_id=args.run_id,
            wait=not args.no_wait,
            mark_active=False,
        )
        exit_code = 0
    elif args.action == "red-herring":
        result = apply(
            RED_HERRING_FLAGS,
            "red-herring",
            run_id=args.run_id,
            wait=not args.no_wait,
        )
        exit_code = 0
    else:
        result = apply(
            FAULT_FLAGS[args.action],
            args.action,
            run_id=args.run_id,
            wait=not args.no_wait,
        )
        exit_code = 0

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def main() -> None:
    try:
        raise SystemExit(run())
    except RECOVERABLE_ERRORS as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
