#!/usr/bin/env python3
"""Inject or clear a Healthcare fault by deploying a new ECS task definition.

Unlike the /fault/* endpoints, this leaves a real RegisterTaskDefinition and
UpdateService trail in CloudTrail, so the deployment that started the incident
is discoverable from change history alone.

Usage:
    inject_deployment_fault.py db-leak              # start the leak via deploy
    inject_deployment_fault.py reset                # clear every fault flag
    inject_deployment_fault.py red-herring          # harmless deploy (decoy)
    inject_deployment_fault.py status               # show current flags
"""

import argparse
import json
import subprocess
import sys

CLUSTER = "RcaAgentDevHealthcare"
SERVICE = "RcaAgentDevHealthcare"
FAMILY = "RcaAgentDevHealthcare"
CONTAINER = "healthcare"

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

# Changing a value the app never branches on. It produces a deployment event
# with no performance effect, so the agent has to use timing and diff content
# rather than "a deploy happened" to pick the culprit.
RED_HERRING_FLAGS = {"LOG_LEVEL": "DEBUG"}


def aws(*args: str) -> dict:
    result = subprocess.run(
        ["aws", *args, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"aws {' '.join(args)} failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def current_task_definition() -> dict:
    return aws("ecs", "describe-task-definition", "--task-definition", FAMILY)["taskDefinition"]


def container_environment(task_definition: dict) -> dict[str, str]:
    for container in task_definition["containerDefinitions"]:
        if container["name"] == CONTAINER:
            return {entry["name"]: entry["value"] for entry in container.get("environment", [])}
    sys.exit(f"container {CONTAINER!r} not found in task definition {FAMILY}")


def register_with_environment(task_definition: dict, overrides: dict[str, str]) -> str:
    for container in task_definition["containerDefinitions"]:
        if container["name"] != CONTAINER:
            continue
        environment = {entry["name"]: entry["value"] for entry in container.get("environment", [])}
        environment.update(overrides)
        container["environment"] = [{"name": k, "value": v} for k, v in sorted(environment.items())]

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
    return registered["taskDefinition"]["taskDefinitionArn"]


def deploy(task_definition_arn: str) -> None:
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


def apply(overrides: dict[str, str], label: str) -> None:
    arn = register_with_environment(current_task_definition(), overrides)
    deploy(arn)
    revision = arn.rsplit("/", 1)[-1]
    print(f"{label}: deployed {revision} with {overrides}")
    print("Rollout and gradual degradation take a few minutes before the alarm fires.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[*FAULT_FLAGS, "reset", "red-herring", "status"],
    )
    action = parser.parse_args().action

    if action == "status":
        environment = container_environment(current_task_definition())
        flags = {k: v for k, v in environment.items() if k.startswith("FAULT_") or k == "DEPLOYED_REVISION"}
        print(json.dumps(flags, indent=2))
        return

    if action == "reset":
        apply(CLEARED_FLAGS, "reset")
        return

    if action == "red-herring":
        apply(RED_HERRING_FLAGS, "red-herring")
        return

    apply(FAULT_FLAGS[action], action)


if __name__ == "__main__":
    main()
