"""Task-owned fixed read-only probe using only the pinned normal deployment's resources."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time


def safe_result(value, operation):
    """Keep only the current native wire; unexpected payload/private fields are never journaled."""
    allowed = (
        {"epoch", "lower_exclusive", "upper_inclusive", "observed_at"}
        if operation == "checkpoint"
        else {
            "epoch",
            "lower_exclusive",
            "upper_inclusive",
            "expected_events",
            "retained_identities",
            "pending_events",
            "missing_measurements",
            "payload_mismatches",
            "matched_measurements",
            "lost_pending_inputs",
            "missing_identities",
            "observed_42703_events",
            "latest_42703_at",
            "complete",
            "reason",
        }
    )
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("invalid native proof fields")
    if "reason" in value and value["reason"] != "epoch or admission bound mismatch":
        raise ValueError("invalid native proof reason")
    return value


class Probe:
    def __init__(self, demo, operation, cohort, wait_seconds):
        """Use the existing 900-second driver wait window by default, including bounded cleanup."""
        self.demo = demo
        self.aws = demo.aws
        self.snapshot = demo.journal.snapshot
        self.budget = wait_seconds or 900
        if self.budget <= 0 or operation not in {"checkpoint", "cohort"}:
            raise ValueError("invalid probe operation/wait window")
        self.operation, self.cohort = operation, cohort
        self.id = os.urandom(16).hex()
        self.run_id = self.snapshot["options"]["run_id"]
        self.cluster = demo.target["cluster"]
        self.definition = self.snapshot["service"]["taskDefinition"]
        self.name = self.snapshot["options"]["container"]
        self.app = next(
            c
            for c in self.snapshot["taskDefinition"]["taskDefinition"][
                "containerDefinitions"
            ]
            if c["name"] == self.name
        )
        self.image = self.app["image"].split("@sha256:")
        if len(self.image) != 2:
            raise ValueError("probe requires the immutable normal image")
        self.image_digest = "sha256:" + self.image[1]
        messages = [row["message"] for row in self.snapshot["normalObservations"]]
        sources = [m for m in messages if m.get("event") == "source_manifest"]
        schemas = {
            m["schema_name"] for m in messages if m.get("event") == "db_schema_snapshot"
        }
        if (
            len(schemas) != 1
            or not sources
            or any(
                not m.get("files", {}).get("backlog_probe.py")
                or m.get("fingerprint")
                != hashlib.sha256(
                    json.dumps(m["files"], sort_keys=True).encode()
                ).hexdigest()
                or m.get("fingerprint") != sources[0].get("fingerprint")
                for m in sources
            )
        ):
            raise ValueError("normal image lacks a verified packaged backlog probe")
        self.fingerprint = sources[0]["fingerprint"]
        self.probe_hash = sources[0]["files"]["backlog_probe.py"]
        self.schema = next(iter(schemas))
        self.database = next(
            e["value"] for e in self.app["environment"] if e["name"] == "DB_NAME"
        )
        self.arns = set()
        self.request = None

    def record(self, kind, value):
        """Bind lifecycle evidence to one probe; persist launch intent before RunTask."""
        return self.demo.journal.append(kind, {"probe_id": self.id, **value})

    def call(self, deadline, service, operation, **payload):
        """Bound every API operation by the same phase deadline, including pagination calls."""
        if time.monotonic() >= deadline:
            raise RuntimeError("probe wait window exhausted")
        return self.aws(service, operation, _deadline=deadline, **payload)

    def tasks(self, arns, deadline):
        """Describe only known or startedBy-discovered task IDs and retain failures as incomplete cleanup."""
        if not arns:
            return []
        result = self.call(
            deadline, "ecs", "describe-tasks", cluster=self.cluster, tasks=sorted(arns)
        )
        if (
            result.get("failures")
            or len(result.get("tasks", [])) != len(arns)
            or {t.get("taskArn") for t in result.get("tasks", [])} != set(arns)
        ):
            raise RuntimeError("probe task identities unavailable")
        return result["tasks"]

    def owned(self, task):
        """Never stop or accept a task whose cluster, pinned definition or startedBy differs."""
        return (
            task.get("clusterArn") == self.cluster
            and task.get("taskDefinitionArn") == self.definition
            and task.get("startedBy") == self.id
        )

    def discover(self, deadline):
        """Find live by startedBy alone, then stopped by family; DescribeTasks must prove exact ownership."""
        queries = [
            {"cluster": self.cluster, "startedBy": self.id},
            {
                "cluster": self.cluster,
                "family": self.definition.rsplit("/", 1)[-1].rsplit(":", 1)[0],
                "desiredStatus": "STOPPED",
            },
        ]
        for query in queries:
            tokens = set()
            while True:
                response = self.call(deadline, "ecs", "list-tasks", **query)
                candidates = response.get("taskArns", [])
                for offset in range(0, len(candidates), 100):
                    for task in self.tasks(candidates[offset : offset + 100], deadline):
                        if self.owned(task):
                            self.arns.add(task["taskArn"])
                token = response.get("nextToken")
                if not token:
                    break
                if token in tokens:
                    raise RuntimeError("probe discovery pagination incomplete")
                tokens.add(token)
                query["nextToken"] = token
            if self.arns:
                return

    def drain(self, deadline):
        """Use only the reserved remainder of the original total window; never reset cleanup time."""
        try:
            while True:
                if not self.arns:
                    self.discover(deadline)
                tasks = self.tasks(self.arns, deadline)
                if any(not self.owned(t) for t in tasks):
                    raise RuntimeError("refusing to stop an unowned task")
                remaining = [t for t in tasks if t.get("lastStatus") != "STOPPED"]
                if tasks and not remaining:
                    self.record(
                        "backlog_probe_stopped", {"task_arns": sorted(self.arns)}
                    )
                    return
                for task in remaining:
                    if task.get("desiredStatus") != "STOPPED":
                        self.call(
                            deadline,
                            "ecs",
                            "stop-task",
                            cluster=self.cluster,
                            task=task["taskArn"],
                            reason="Owned read-only backlog probe cleanup",
                        )
                if time.monotonic() >= deadline:
                    raise RuntimeError("probe cleanup deadline exhausted")
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        except (OSError, RuntimeError):
            self.record(
                "backlog_probe_cleanup_unconfirmed",
                {"task_arns": sorted(self.arns), "status": "UNKNOWN"},
            )
            raise RuntimeError(
                "probe cleanup UNKNOWN; no proof or new task may be accepted"
            ) from None

    def logs(self, task, deadline):
        """Accept exactly one source/identity-bound JSON record from this task's own log stream."""
        options = self.app["logConfiguration"]["options"]
        stream = (
            options["awslogs-stream-prefix"]
            + "/"
            + self.name
            + "/"
            + task["taskArn"].rsplit("/", 1)[-1]
        )
        while True:
            args = {
                "logGroupName": options["awslogs-group"],
                "logStreamName": stream,
                "startFromHead": True,
            }
            messages, tokens = [], set()
            while True:
                response = self.call(deadline, "logs", "get-log-events", **args)
                for event in response.get("events", []):
                    try:
                        value = json.loads(event["message"])
                    except (ValueError, TypeError):
                        continue
                    if (
                        isinstance(value, dict)
                        and value.get("probe_id") == self.id
                        and value.get("event") == "vital_backlog_proof"
                    ):
                        messages.append(value)
                token = response.get("nextForwardToken")
                if not token or token == args.get("nextToken"):
                    break
                if token in tokens:
                    raise RuntimeError("probe log pagination incomplete")
                tokens.add(token)
                args["nextToken"] = token
            if messages:
                first = messages[0]
                if any(m != first for m in messages):
                    raise RuntimeError("conflicting probe summaries")
                if (
                    first.get("run_id") != self.run_id
                    or first.get("operation") != self.operation
                    or first.get("source_fingerprint") != self.fingerprint
                    or first.get("probe_source_sha256") != self.probe_hash
                    or first.get("database") != self.database
                    or first.get("schema") != self.schema
                ):
                    raise RuntimeError(
                        "probe summary differs from pinned run/source/database"
                    )
                if set(first) != {
                    "event",
                    "probe_id",
                    "run_id",
                    "operation",
                    "source_fingerprint",
                    "probe_source_sha256",
                    "observed_at",
                    "database",
                    "schema",
                    "result",
                }:
                    raise RuntimeError("unexpected probe output fields")
                safe_result(first.get("result"), self.operation)
                return first, {
                    "log_group": options["awslogs-group"],
                    "log_stream": stream,
                }
            if time.monotonic() >= deadline:
                raise RuntimeError("completed probe has no verifiable output")
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def run(self):
        """Launch one idempotent fixed command, bind actual image/exit, and clean every unsuccessful path."""
        command = [
            "python",
            "-m",
            "test_service.backlog_probe",
            "--operation",
            self.operation,
            "--probe-id",
            self.id,
            "--run-id",
            self.run_id,
            "--schema",
            self.schema,
        ]
        if self.cohort is not None:
            command += [
                "--epoch",
                self.cohort["epoch"],
                "--lower-exclusive",
                str(self.cohort["lower_exclusive"]),
                "--upper-inclusive",
                str(self.cohort["upper_inclusive"]),
            ]
        service = self.snapshot["service"]
        self.request = {
            "cluster": self.cluster,
            "taskDefinition": self.definition,
            "count": 1,
            "startedBy": self.id,
            "clientToken": self.id,
            "enableExecuteCommand": False,
            "networkConfiguration": copy.deepcopy(service["networkConfiguration"]),
            "overrides": {
                "containerOverrides": [{"name": self.name, "command": command}]
            },
            "tags": [
                {"key": "RealisticDemoRunId", "value": self.run_id},
                {"key": "BacklogProbeId", "value": self.id},
            ],
        }
        if service.get("capacityProviderStrategy"):
            self.request["capacityProviderStrategy"] = copy.deepcopy(
                service["capacityProviderStrategy"]
            )
        else:
            self.request["launchType"] = service.get("launchType", "FARGATE")
        if service.get("platformVersion"):
            self.request["platformVersion"] = service["platformVersion"]
        terminal = False
        attempted = False
        self.record(
            "backlog_probe_intent",
            {"request": self.request, "operation": self.operation},
        )
        total_deadline = time.monotonic() + self.budget
        # Reserve at most the existing CLI-call window, never an additional wait budget.
        cleanup_reserve = min(90, self.budget / 2)
        deadline = total_deadline - cleanup_reserve
        self.record(
            "backlog_probe_budget",
            {
                "total_wait_seconds": self.budget,
                "cleanup_reserved_seconds": cleanup_reserve,
            },
        )
        try:
            attempted = True
            try:
                response = self.call(deadline, "ecs", "run-task", **self.request)
            except (OSError, RuntimeError):
                # Same logical launch/token, not a new task or a retry of DB writes.
                try:
                    response = self.call(deadline, "ecs", "run-task", **self.request)
                except (OSError, RuntimeError):
                    self.discover(deadline)
                    if len(self.arns) != 1:
                        raise RuntimeError("RunTask outcome unknown") from None
                    response = {"tasks": [{"taskArn": arn} for arn in self.arns]}
            self.arns.update(t["taskArn"] for t in response.get("tasks", []))
            if response.get("failures") and not self.arns:
                attempted = False
                self.record(
                    "backlog_probe_stopped", {"task_arns": [], "launch_rejected": True}
                )
            if response.get("failures") or len(self.arns) != 1:
                raise RuntimeError(
                    "probe launch failed or returned an unexpected task set"
                )
            self.record("backlog_probe_started", {"task_arns": sorted(self.arns)})
            while True:
                tasks = self.tasks(self.arns, deadline)
                if len(tasks) != 1 or not self.owned(tasks[0]):
                    raise RuntimeError("probe task ownership mismatch")
                task = tasks[0]
                if task.get("lastStatus") == "STOPPED":
                    terminal = True
                    self.record(
                        "backlog_probe_stopped", {"task_arns": sorted(self.arns)}
                    )
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("owned probe exceeded its wait window")
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            app = next(
                (c for c in task.get("containers", []) if c.get("name") == self.name),
                {},
            )
            if app.get("exitCode") != 0 or app.get("imageDigest") != self.image_digest:
                raise RuntimeError("probe normal image or successful exit not proven")
            value, log = self.logs(task, deadline)
            result = {
                "transport": "ecs_standalone_readonly",
                "task_arn": task["taskArn"],
                "task_definition": self.definition,
                "image_digest": self.image_digest,
                "exit_code": 0,
                "probe_id": self.id,
                "run_id": self.run_id,
                "source_fingerprint": self.fingerprint,
                "result": value["result"],
                **log,
            }
            self.record("backlog_probe_result", result)
            return result
        finally:
            if attempted and not terminal:
                self.drain(total_deadline)


def run_probe(demo, operation, cohort=None, wait_seconds=0):
    """Refuse a new launch while a prior owned probe lacks terminal evidence."""
    stopped = {
        e["data"]["probe_id"] for e in demo.journal.find("backlog_probe_stopped")
    }
    for event in demo.journal.find("backlog_probe_intent"):
        if event["data"]["probe_id"] not in stopped:
            raise RuntimeError(
                "previous probe cleanup is unconfirmed; do not launch another task"
            )
    return Probe(demo, operation, cohort, wait_seconds).run()
