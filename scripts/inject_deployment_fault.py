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
import json
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

_aws_context: list[str] = []


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
        sys.exit(f"aws {' '.join(args)} failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def service_task_definition_arn() -> str:
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
        sys.exit(f"ECS service {CLUSTER}/{SERVICE} was not found")
    return str(services[0]["taskDefinition"])


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
    sys.exit(f"container {CONTAINER!r} not found in task definition {FAMILY}")


def register_with_environment(
    task_definition: dict[str, Any],
    overrides: dict[str, str],
) -> str:
    found = False
    for container in task_definition["containerDefinitions"]:
        if container["name"] != CONTAINER:
            continue
        found = True
        environment = {
            entry["name"]: entry["value"]
            for entry in container.get("environment", [])
        }
        environment.update(overrides)
        container["environment"] = [
            {"name": key, "value": value}
            for key, value in sorted(environment.items())
        ]
    if not found:
        sys.exit(f"container {CONTAINER!r} not found in task definition {FAMILY}")

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
            sys.exit(
                "ECS service stabilized on an unexpected task definition: "
                f"{actual} != {task_definition_arn}"
            )


def apply(
    overrides: dict[str, str],
    label: str,
    *,
    run_id: str,
    wait: bool,
) -> dict[str, Any]:
    started_at = utc_now()
    deployment_overrides = {**overrides, "RCA_TEST_RUN_ID": run_id}
    arn = register_with_environment(
        current_task_definition(),
        deployment_overrides,
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
    }


def wait_for_alarms_ok(*, timeout_seconds: int) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, str] = {}
    while time.monotonic() < deadline:
        response = aws(
            "cloudwatch",
            "describe-alarms",
            "--alarm-names",
            *ALARM_NAMES,
        )
        latest = {
            str(alarm["AlarmName"]): str(alarm["StateValue"])
            for alarm in response.get("MetricAlarms", [])
        }
        if all(latest.get(name) == "OK" for name in ALARM_NAMES):
            return latest
        time.sleep(10)
    sys.exit(
        f"alarms did not return to OK within {timeout_seconds}s: "
        f"{json.dumps(latest, sort_keys=True)}"
    )


def restore_parameter_group(
    group_name: str,
    *,
    delete_groups: list[str],
) -> dict[str, Any]:
    response = aws(
        "rds",
        "describe-db-instances",
        "--db-instance-identifier",
        DB_INSTANCE,
    )
    instance = response["DBInstances"][0]
    current = instance["DBParameterGroups"][0]["DBParameterGroupName"]
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

        refreshed = aws(
            "rds",
            "describe-db-instances",
            "--db-instance-identifier",
            DB_INSTANCE,
        )["DBInstances"][0]
        parameter = refreshed["DBParameterGroups"][0]
        if parameter.get("ParameterApplyStatus") == "pending-reboot":
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

    deleted: list[str] = []
    for candidate in delete_groups:
        if candidate == group_name:
            sys.exit("refusing to delete the restored DB parameter group")
        aws(
            "rds",
            "delete-db-parameter-group",
            "--db-parameter-group-name",
            candidate,
        )
        deleted.append(candidate)
    return {
        "previousParameterGroup": current,
        "restoredParameterGroup": group_name,
        "changed": changed,
        "deletedParameterGroups": deleted,
    }


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
    args = parser.parse_args()
    if args.action != "status" and not args.run_id:
        parser.error("--run-id is required for mutating actions")
    if args.run_id is not None:
        args.run_id = args.run_id.strip()
        if not args.run_id:
            parser.error("--run-id must not be blank")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    if args.action != "cleanup" and (
        args.restore_db_parameter_group or args.delete_db_parameter_group
    ):
        parser.error("DB parameter group options are valid only with cleanup")
    if args.delete_db_parameter_group and not args.restore_db_parameter_group:
        parser.error(
            "--delete-db-parameter-group requires --restore-db-parameter-group"
        )
    return args


def main() -> None:
    args = parse_args()
    if args.profile:
        _aws_context.extend(["--profile", args.profile])
    if args.region:
        _aws_context.extend(["--region", args.region])

    if args.action == "status":
        task_definition = current_task_definition()
        environment = container_environment(task_definition)
        result = {
            "action": "status",
            "taskDefinitionArn": task_definition["taskDefinitionArn"],
            "flags": {
                key: value
                for key, value in environment.items()
                if key.startswith("FAULT_")
                or key in {"DEPLOYED_REVISION", "RCA_TEST_RUN_ID"}
            },
        }
    elif args.action == "cleanup":
        result = apply(
            CLEARED_FLAGS,
            "cleanup",
            run_id=args.run_id,
            wait=not args.no_wait,
        )
        if args.restore_db_parameter_group:
            result["database"] = restore_parameter_group(
                args.restore_db_parameter_group,
                delete_groups=args.delete_db_parameter_group,
            )
        result["alarmStates"] = wait_for_alarms_ok(
            timeout_seconds=args.timeout_seconds,
        )
        result["clean"] = True
    elif args.action == "reset":
        result = apply(
            CLEARED_FLAGS,
            "reset",
            run_id=args.run_id,
            wait=not args.no_wait,
        )
    elif args.action == "red-herring":
        result = apply(
            RED_HERRING_FLAGS,
            "red-herring",
            run_id=args.run_id,
            wait=not args.no_wait,
        )
    else:
        result = apply(
            FAULT_FLAGS[args.action],
            args.action,
            run_id=args.run_id,
            wait=not args.no_wait,
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
