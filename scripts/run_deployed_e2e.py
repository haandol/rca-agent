#!/usr/bin/env python3
"""Run deployed RCA validation inside a mandatory cleanup boundary.

The caller owns the run id and validation command. This driver records the
original Healthcare environment and DB state before mutation, deploys the
red herring and DB leak, runs the validation command, and always invokes
cleanup.

Usage:
    run_deployed_e2e.py --run-id <id> --manifest <path> -- <command> [args...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_FAULT_SCRIPT = Path(__file__).with_name("inject_deployment_fault.py")
PARAMETER_GROUP_RUN_TAG = "RCA_TEST_RUN_ID"
PARAMETER_GROUP_PROVENANCE_TAG = "RCA_TEST_PROVENANCE"
PARAMETER_GROUP_PROVENANCE = "inject_deployment_fault.py:db-leak"
INITIAL_CLEAN_CHECKS = (
    "faultFlagsClear",
    "serviceStable",
    "databaseAvailable",
    "databaseParameterGroupInSync",
    "alarmsOk",
    "noActiveRun",
)
RECOVERABLE_ERRORS = (
    OSError,
    KeyError,
    RuntimeError,
    subprocess.SubprocessError,
    TypeError,
    ValueError,
)


class DriverError(RuntimeError):
    """The orchestration boundary could not complete an operation."""


class RunInterrupted(DriverError):
    """The driver received SIGINT or SIGTERM."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"received {signal.Signals(signum).name}")
        self.signum = signum


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(manifest, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def parse_json_output(
    result: subprocess.CompletedProcess[str],
    *,
    operation: str,
) -> dict[str, Any]:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DriverError(
            f"{operation} failed with exit code {result.returncode}: {detail}"
        )
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DriverError(f"{operation} returned invalid JSON") from error
    if not isinstance(output, dict):
        raise DriverError(f"{operation} returned a non-object JSON value")
    return output


def stop_process_group(child: subprocess.Popen[Any] | None) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.communicate(timeout=10)
    except ProcessLookupError:
        child.wait()
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()


def fault_command(
    args: argparse.Namespace,
    action: str,
    *extra: str,
) -> list[str]:
    command = [
        sys.executable,
        str(args.fault_script),
        action,
        "--json",
    ]
    if action != "status":
        command.extend(["--run-id", args.run_id])
    if args.profile:
        command.extend(["--profile", args.profile])
    if args.region:
        command.extend(["--region", args.region])
    command.extend(extra)
    return command


class FaultCommandRunner:
    """Run one fault CLI child at a time and reap interrupted process groups."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.active_child: subprocess.Popen[str] | None = None

    def invoke(self, action: str, *extra: str) -> dict[str, Any]:
        command = fault_command(self.args, action, *extra)
        child = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.active_child = child
        try:
            stdout, stderr = child.communicate()
        except (RunInterrupted, OSError, subprocess.SubprocessError):
            stop_process_group(child)
            raise
        finally:
            self.active_child = None
        result = subprocess.CompletedProcess(
            command,
            child.returncode,
            stdout,
            stderr,
        )
        return parse_json_output(result, operation=action)


def managed_value(
    status: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    value = status.get("environment", {}).get(name)
    if not isinstance(value, dict) or "present" not in value:
        raise DriverError(f"status is missing environment state for {name}")
    return value


def original_state(status: dict[str, Any]) -> dict[str, Any]:
    database = status.get("database")
    environment = status.get("environment")
    if not isinstance(database, dict) or not isinstance(environment, dict):
        raise DriverError("status is missing database or environment state")
    if not database.get("parameterGroup"):
        raise DriverError("status is missing the active DB parameter group")
    if not isinstance(database.get("parameterGroups"), list):
        raise DriverError("status is missing the DB parameter group inventory")
    managed_value(status, "LOG_LEVEL")
    return {
        "taskDefinitionArn": status.get("taskDefinitionArn"),
        "environment": environment,
        "dbParameterGroup": database["parameterGroup"],
        "dbParameterGroups": database["parameterGroups"],
    }


def validate_initial_status(status: dict[str, Any]) -> None:
    checks = status.get("checks")
    if not isinstance(checks, dict):
        raise DriverError("initial status is missing clean-state checks")
    failed = [name for name in INITIAL_CLEAN_CHECKS if checks.get(name) is not True]
    if status.get("clean") is not True or failed:
        detail = ", ".join(failed) if failed else "status.clean"
        raise DriverError(f"initial status is not clean: {detail}")
    if status.get("activeRunId") is not None:
        raise DriverError(
            f"initial status has an active run: {status['activeRunId']!r}"
        )
    database = status.get("database")
    if not isinstance(database, dict):
        raise DriverError("initial status is missing database state")
    if database.get("parameterApplyStatus") != "in-sync":
        raise DriverError("initial DB parameter group is not in-sync")


def cleanup_arguments(
    manifest: dict[str, Any],
    *,
    owned_parameter_group_proofs: list[dict[str, Any]],
    timeout_seconds: int,
) -> list[str]:
    original = manifest["original"]
    arguments = [
        "--timeout-seconds",
        str(timeout_seconds),
        "--restore-db-parameter-group",
        str(original["dbParameterGroup"]),
    ]
    log_level = original["environment"]["LOG_LEVEL"]
    if log_level["present"]:
        arguments.extend(["--restore-log-level", str(log_level["value"])])
    else:
        arguments.append("--remove-log-level")
    for proof in owned_parameter_group_proofs:
        arguments.extend(
            [
                "--delete-db-parameter-group",
                json.dumps(proof, separators=(",", ":"), sort_keys=True),
            ]
        )
    return arguments


def parameter_group_name_prefix(run_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")[:32]
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"rca-e2e-{slug or 'run'}-{digest}-"


def owned_parameter_group_proofs(
    fault_result: dict[str, Any],
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    owned_resources = fault_result.get("ownedResources")
    if not isinstance(owned_resources, dict):
        raise DriverError("db-leak result is missing ownedResources")
    proofs = owned_resources.get("dbParameterGroups")
    if not isinstance(proofs, list):
        raise DriverError(
            "db-leak result is missing DB parameter group ownership proofs"
        )

    validated: list[dict[str, Any]] = []
    names: set[str] = set()
    expected_tags = {
        PARAMETER_GROUP_RUN_TAG: run_id,
        PARAMETER_GROUP_PROVENANCE_TAG: PARAMETER_GROUP_PROVENANCE,
    }
    for proof in proofs:
        if not isinstance(proof, dict):
            raise DriverError("DB parameter group ownership proof must be an object")
        name = proof.get("name")
        if not isinstance(name, str) or not name.startswith(
            parameter_group_name_prefix(run_id)
        ):
            raise DriverError(
                "DB parameter group ownership proof has an invalid run-scoped name"
            )
        if name in names:
            raise DriverError(
                f"duplicate DB parameter group ownership proof: {name}"
            )
        if proof.get("runId") != run_id:
            raise DriverError("DB parameter group ownership proof runId mismatch")
        if proof.get("provenance") != PARAMETER_GROUP_PROVENANCE:
            raise DriverError(
                "DB parameter group ownership proof provenance mismatch"
            )
        tags = proof.get("tags")
        if not isinstance(tags, dict) or any(
            tags.get(key) != value for key, value in expected_tags.items()
        ):
            raise DriverError("DB parameter group ownership proof tags mismatch")
        names.add(name)
        validated.append(proof)
    return sorted(validated, key=lambda proof: str(proof["name"]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--fault-script",
        type=Path,
        default=DEFAULT_FAULT_SCRIPT,
    )
    parser.add_argument("--profile")
    parser.add_argument("--region")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--red-herring-delay-seconds", type=float, default=150)
    parser.add_argument("validation_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.run_id = args.run_id.strip()
    if not args.run_id:
        parser.error("--run-id must not be blank")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    if args.red_herring_delay_seconds < 0:
        parser.error("--red-herring-delay-seconds must not be negative")
    if args.validation_command and args.validation_command[0] == "--":
        args.validation_command = args.validation_command[1:]
    if not args.validation_command:
        parser.error("a validation command is required after --")
    return args


def run() -> int:
    args = parse_args()
    fault_runner = FaultCommandRunner(args)
    initial_status = fault_runner.invoke("status")
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "createdAt": utc_now(),
        "original": original_state(initial_status),
        "validationCommand": args.validation_command,
        "events": [
            {
                "at": utc_now(),
                "name": "initial-status",
                "result": initial_status,
            }
        ],
    }
    write_manifest(args.manifest, manifest)
    try:
        validate_initial_status(initial_status)
    except DriverError as error:
        manifest["preflightError"] = {
            "at": utc_now(),
            "type": type(error).__name__,
            "message": str(error),
        }
        manifest["completedAt"] = utc_now()
        manifest["exitCode"] = 1
        write_manifest(args.manifest, manifest)
        return 1

    interrupted_signal: list[int | None] = [None]
    validation_child: subprocess.Popen[Any] | None = None
    outcome_code = 0
    owned_group_proofs: list[dict[str, Any]] = []

    def interrupt_handler(signum: int, _frame: object) -> None:
        interrupted_signal[0] = signum
        raise RunInterrupted(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupt_handler)

    try:
        red_herring = fault_runner.invoke("red-herring")
        manifest["events"].append(
            {
                "at": utc_now(),
                "name": "red-herring",
                "result": red_herring,
            }
        )
        write_manifest(args.manifest, manifest)

        time.sleep(args.red_herring_delay_seconds)

        fault = fault_runner.invoke("db-leak")
        manifest["events"].append(
            {
                "at": utc_now(),
                "name": "db-leak",
                "result": fault,
            }
        )
        owned_group_proofs = owned_parameter_group_proofs(
            fault,
            run_id=args.run_id,
        )
        write_manifest(args.manifest, manifest)

        child_environment = {
            **os.environ,
            "RCA_E2E_RUN_ID": args.run_id,
            "RCA_E2E_MANIFEST": str(args.manifest.resolve()),
            "RCA_E2E_STARTED_AT": manifest["createdAt"],
            "RCA_E2E_FAULT_COMPLETED_AT": str(fault["completedAt"]),
            "RCA_E2E_EVIDENCE_DIR": str(args.manifest.resolve().parent),
        }
        validation_child = subprocess.Popen(
            args.validation_command,
            env=child_environment,
            start_new_session=True,
        )
        validation_code = validation_child.wait()
        manifest["validation"] = {
            "completedAt": utc_now(),
            "exitCode": validation_code,
        }
        write_manifest(args.manifest, manifest)
        if validation_code != 0:
            outcome_code = validation_code
    except RunInterrupted as error:
        outcome_code = 128 + error.signum
        manifest["interrupted"] = {
            "at": utc_now(),
            "signal": signal.Signals(error.signum).name,
        }
        write_manifest(args.manifest, manifest)
    except RECOVERABLE_ERRORS as error:
        outcome_code = 1
        manifest["orchestrationError"] = {
            "at": utc_now(),
            "type": type(error).__name__,
            "message": str(error),
        }
        write_manifest(args.manifest, manifest)
    finally:
        stop_process_group(validation_child)

        try:
            pre_cleanup_status = fault_runner.invoke("status")
            manifest["preCleanup"] = {
                "capturedAt": utc_now(),
                "ownedDbParameterGroups": [
                    proof["name"] for proof in owned_group_proofs
                ],
                "ownedDbParameterGroupProofs": owned_group_proofs,
                "status": pre_cleanup_status,
            }
            write_manifest(args.manifest, manifest)
        except RunInterrupted as error:
            manifest["interrupted"] = {
                "at": utc_now(),
                "signal": signal.Signals(error.signum).name,
            }
            write_manifest(args.manifest, manifest)
        except RECOVERABLE_ERRORS as error:
            manifest["preCleanupError"] = {
                "at": utc_now(),
                "type": type(error).__name__,
                "message": str(error),
            }
            write_manifest(args.manifest, manifest)

        # Once cleanup starts, further terminal signals are recorded but deferred.
        def defer_signal(signum: int, _frame: object) -> None:
            interrupted_signal[0] = interrupted_signal[0] or signum

        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, defer_signal)

        try:
            cleanup_result = fault_runner.invoke(
                "cleanup",
                *cleanup_arguments(
                    manifest,
                    owned_parameter_group_proofs=owned_group_proofs,
                    timeout_seconds=args.timeout_seconds,
                ),
            )
            manifest["cleanup"] = {
                "completedAt": utc_now(),
                "result": cleanup_result,
            }
            if not cleanup_result.get("clean"):
                outcome_code = outcome_code or 1
        except RECOVERABLE_ERRORS as error:
            outcome_code = outcome_code or 1
            manifest["cleanupError"] = {
                "at": utc_now(),
                "type": type(error).__name__,
                "message": str(error),
            }

        if interrupted_signal[0] is not None:
            outcome_code = 128 + interrupted_signal[0]
        manifest["completedAt"] = utc_now()
        manifest["exitCode"] = outcome_code
        write_manifest(args.manifest, manifest)

    return outcome_code


def main() -> None:
    try:
        raise SystemExit(run())
    except DriverError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
