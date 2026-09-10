#!/usr/bin/env python3
"""Plan, apply, inspect and restore owned Healthcare demo changes.

AWS CLI v2 and Python 3.11+ are required. Planning only reads AWS. All commands
retain hash-linked, create-only evidence under --journal-root (use the SAME
shared directory for every operator). See run_realistic_demo.html for examples.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

OWNER = "RealisticDemoRunId"
PROOF = "RealisticDemoJournal"
MAINTENANCE_COMMAND = [
    "python",
    "-m",
    "test_service.maintenance",
    "--run-id",
    "{run_id}",
    "--hold-seconds",
    "{hold_seconds}",
    "--schema",
    "{schema}",
]
RECOVERABLE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    subprocess.SubprocessError,
    KeyboardInterrupt,
)
SETTINGS = (
    "TRAFFIC_ENABLED",
    "TRAFFIC_INTERVAL_SECONDS",
    "TRAFFIC_MAX_CONCURRENCY",
    "TRAFFIC_QUERY_LIMIT",
    "TRAFFIC_PATIENT_ID",
    "TRAFFIC_SEED",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT_SECONDS",
    "DB_STATEMENT_TIMEOUT_MS",
    "DB_OBSERVABILITY_ENABLED",
    "DB_OBSERVABILITY_INTERVAL_SECONDS",
    "DEPLOYED_REVISION",
    "RCA_TEST_RUN_ID",
    "RCA_TEST_PHASE",
)
SERVICE_SETTINGS = (
    "desiredCount",
    "networkConfiguration",
    "deploymentConfiguration",
    "capacityProviderStrategy",
    "launchType",
    "platformVersion",
    "enableExecuteCommand",
    "schedulingStrategy",
    "deploymentController",
    "loadBalancers",
    "serviceRegistries",
    "placementConstraints",
    "placementStrategy",
    "enableECSManagedTags",
    "propagateTags",
    "healthCheckGracePeriodSeconds",
    "serviceConnectConfiguration",
    "volumeConfigurations",
    "vpcLatticeConfigurations",
    "availabilityZoneRebalancing",
)
ALARM_SETTINGS = (
    "AlarmName",
    "Namespace",
    "MetricName",
    "Dimensions",
    "Unit",
    "Statistic",
    "ExtendedStatistic",
    "Period",
    "EvaluationPeriods",
    "DatapointsToAlarm",
    "Threshold",
    "ComparisonOperator",
    "TreatMissingData",
    "Metrics",
    "AlarmActions",
    "OKActions",
    "ActionsEnabled",
)
READ_ONLY_TD = (
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
    "deregisteredAt",
)


def now():
    """Return an aware UTC timestamp suitable for retained evidence."""
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    """Hash a canonical JSON value for journal integrity and ownership."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def maintenance_command(options):
    """Build only the bounded maintenance module command, including for old journals."""
    if (
        "maintenance_command" in options
        and options["maintenance_command"] != MAINTENANCE_COMMAND
    ):
        raise RuntimeError(
            "journal records an unsupported maintenance command; restore only"
        )
    run_id = options["run_id"]
    hold_seconds = options["hold_seconds"]
    schema = options.get("maintenance_schema", "public")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", run_id):
        raise ValueError("RunId must be 1..48 letters, digits, underscores or hyphens")
    if type(hold_seconds) not in (int, float) or not 0 < hold_seconds <= 7200:
        raise ValueError("hold-seconds must be finite, positive and <=7200")
    if not isinstance(schema, str) or not re.fullmatch(
        r"[a-z_][a-z0-9_]{0,62}", schema
    ):
        raise ValueError("maintenance-schema must be a safe PostgreSQL identifier")
    return [
        part.format(run_id=run_id, hold_seconds=hold_seconds, schema=schema)
        for part in MAINTENANCE_COMMAND
    ]


def atomic_create(path, value):
    """Publish a complete fsynced JSON file without replacing existing evidence."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


class Journal:
    """Create-only hash-linked event files; callers hold the service file lock."""

    def __init__(self, path):
        """Load and validate all published events, ignoring unpublished temp files."""
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.events = []
        for file in sorted(self.path.glob("[0-9]*.json")):
            event = json.loads(file.read_text())
            expected = f"{len(self.events):06d}.json"
            previous = self.events[-1]["hash"] if self.events else None
            if (
                file.name != expected
                or event["previous"] != previous
                or event["hash"]
                != digest({k: v for k, v in event.items() if k != "hash"})
            ):
                raise RuntimeError("journal integrity mismatch; refusing mutation")
            self.events.append(event)

    def append(self, kind, data):
        """Durably append an event before any associated external side effect."""
        event = {
            "kind": kind,
            "at": now(),
            "data": copy.deepcopy(data),
            "previous": self.events[-1]["hash"] if self.events else None,
        }
        event["hash"] = digest(event)
        atomic_create(self.path / f"{len(self.events):06d}.json", event)
        self.events.append(event)
        return event

    def find(self, kind):
        """Return events of a given kind in chronological order."""
        return [event for event in self.events if event["kind"] == kind]

    @property
    def snapshot(self):
        """Return the original pre-mutation snapshot without modifying it."""
        if not self.events or self.events[0]["kind"] != "snapshot":
            raise RuntimeError("plan is required before apply/status/restore")
        return copy.deepcopy(self.events[0]["data"])


class Aws:
    """Small injectable AWS CLI adapter; command arguments never use a shell."""

    def __init__(self, region, profile=None):
        """Pin every command to the caller's region and optional profile."""
        self.context = ["--region", region]
        if profile:
            self.context += ["--profile", profile]

    def __call__(self, service, operation, /, **payload):
        """Execute one CLI operation and reject transport or partial failures."""
        try:
            process = subprocess.run(
                [
                    "aws",
                    *self.context,
                    service,
                    operation,
                    "--cli-input-json",
                    json.dumps(payload),
                    "--output",
                    "json",
                    "--no-cli-pager",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # TimeoutExpired.__str__ includes argv, which can contain environment values.
            raise RuntimeError(
                f"AWS {service}/{operation} timed out; outcome uncertain"
            ) from None
        if process.returncode:
            # Do not print the input (task definitions can contain literal secrets).
            raise RuntimeError(f"AWS {service}/{operation}: {process.stderr.strip()}")
        result = json.loads(process.stdout or "{}")
        if result.get("failures"):
            raise RuntimeError(
                f"AWS {service}/{operation} partial failure: {result['failures']}"
            )
        return result


def container(task_definition, name):
    """Select exactly the application container, rejecting ambiguous definitions."""
    matches = [c for c in task_definition["containerDefinitions"] if c["name"] == name]
    if len(matches) != 1:
        raise RuntimeError("application container missing or ambiguous")
    return matches[0]


def env_map(task_definition, name):
    """Read literal environment values without resolving secret references."""
    return {
        e["name"]: e["value"]
        for e in container(task_definition, name).get("environment", [])
    }


def image_repository(image):
    """Remove a tag/digest without stripping a registry's optional port."""
    image = image.split("@")[0]
    return image[: image.rfind(":")] if image.rfind(":") > image.rfind("/") else image


def immutable_baseline_image(task_definition, tasks, name):
    """Require the original definition itself to pin the observed application digest."""
    image = container(task_definition, name)["image"]
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image):
        raise RuntimeError(
            "original application image must use repository@sha256:digest; prepare an immutable r1 baseline before plan"
        )
    expected = image.split("@")[1]
    if not tasks or any(
        [c.get("imageDigest") for c in task.get("containers", []) if c["name"] == name]
        != [expected]
        for task in tasks
    ):
        raise RuntimeError(
            "original image reference does not match every running application digest"
        )
    return image


def settings(service):
    """Preserve settings and their absence for exact drift detection."""
    return {key: service.get(key) for key in SERVICE_SETTINGS}


def stable(service, arn):
    """Require one completed deployment with all desired tasks running."""
    deployments = service.get("deployments", [])
    return (
        service.get("status") == "ACTIVE"
        and service["taskDefinition"] == arn
        and service.get("desiredCount", 0) > 0
        and service.get("runningCount") == service["desiredCount"]
        and service.get("pendingCount") == 0
        and len(deployments) == 1
        and deployments[0].get("taskDefinition") == arn
        and deployments[0].get("rolloutState") == "COMPLETED"
    )


class Demo:
    """Orchestrate one caller-owned scenario and independently verify recovery."""

    def __init__(self, aws, journal, target):
        """Bind an injectable AWS adapter to a journal and explicit target."""
        self.aws, self.journal, self.target = aws, journal, target
        self.journal_errors = []
        self.cleanup_result = None
        self.discovered_task_arns = set()

    def record(self, kind, data, *, recovery=False):
        """Keep new intents strict while neither storage nor diagnostic I/O blocks cleanup."""
        try:
            return self.journal.append(kind, data)
        except RECOVERABLE_ERRORS as error:
            failure = {
                "step": "journal",
                "event": kind,
                "error": f"journal append failed: {type(error).__name__}",
            }
            self.journal_errors.append(failure)
            # Do not expose event payloads or arbitrary filesystem exception text.
            try:
                print(
                    json.dumps(failure | {"recoveryVerified": False}), file=sys.stderr
                )
            except (OSError, ValueError) as diagnostic_error:
                # The returned in-memory evidence survives even a closed/full stderr.
                failure["diagnosticErrorType"] = type(diagnostic_error).__name__
            if not recovery:
                raise
            return None

    def evidence_result(self, result):
        """Keep any journal failure sticky in this command's returned recovery result."""
        result["journalErrors"] = copy.deepcopy(self.journal_errors)
        if self.journal_errors:
            result["recoveryVerified"] = False
            if "restorationState" in result:
                result["restorationState"] = "pending"
        return result

    def service(self):
        """Describe the single target service, including live ownership tags."""
        result = self.aws(
            "ecs",
            "describe-services",
            cluster=self.target["cluster"],
            services=[self.target["service"]],
            include=["TAGS"],
        )
        if len(result.get("services", [])) != 1:
            raise RuntimeError("target service missing")
        return result["services"][0]

    def task_definition(self, arn):
        """Read a task definition and its ownership tags."""
        return self.aws(
            "ecs", "describe-task-definition", taskDefinition=arn, include=["TAGS"]
        )

    def tasks(self, **filters):
        """List tasks with CLI pagination, then describe in API-sized batches."""
        if "serviceName" in filters:
            filters["serviceName"] = filters["serviceName"].rsplit("/", 1)[-1]
        arns = self.aws("ecs", "list-tasks", cluster=self.target["cluster"], **filters)[
            "taskArns"
        ]
        return self.describe_tasks(arns)

    def describe_tasks(self, arns):
        """Capture task digests, tags, lifecycle and exit details for evidence."""
        result = []
        for offset in range(0, len(arns), 100):
            result.extend(
                self.aws(
                    "ecs",
                    "describe-tasks",
                    cluster=self.target["cluster"],
                    tasks=arns[offset : offset + 100],
                    include=["TAGS"],
                )["tasks"]
            )
        return result

    def alarms(self, names):
        """Require every configured alarm to exist; missing is never healthy."""
        result = self.aws("cloudwatch", "describe-alarms", AlarmNames=names)[
            "MetricAlarms"
        ]
        if {alarm["AlarmName"] for alarm in result} != set(names):
            raise RuntimeError("required symptom/evidence alarm missing")
        return result

    def plan(self, options):
        """Snapshot a clean stable service and freeze every scenario parameter."""
        if self.journal.events:
            raise RuntimeError(
                "RunId already has immutable evidence; choose a new RunId"
            )
        service = self.service()
        tags = {t["key"]: t["value"] for t in service.get("tags", [])}
        original = self.task_definition(service["taskDefinition"])
        environment = env_map(original["taskDefinition"], options["container"])
        dirty_flags = (
            environment.get("FAULT_DB_LEAK", "false").lower() != "false"
            or float(environment.get("FAULT_SLOW_QUERY_MS", "0")) != 0
            or float(environment.get("FAULT_ERROR_RATE", "0")) != 0
        )
        if (
            OWNER in tags
            or PROOF in tags
            or environment.get("RCA_TEST_RUN_ID")
            or dirty_flags
            or not stable(service, service["taskDefinition"])
        ):
            raise RuntimeError("dirty or owned service; refusing plan")
        if (
            service.get("deploymentController", {}).get("type", "ECS") != "ECS"
            or service.get("schedulingStrategy", "REPLICA") != "REPLICA"
            or service.get("networkConfiguration", {})
            .get("awsvpcConfiguration", {})
            .get("assignPublicIp")
            != "DISABLED"
        ):
            raise RuntimeError("only private ECS rolling services are supported")
        secret_names = {
            s["name"]
            for s in container(original["taskDefinition"], options["container"]).get(
                "secrets", []
            )
        }
        if secret_names.intersection(SETTINGS):
            raise RuntimeError(
                "controlled setting hidden in secrets; cannot establish baseline"
            )
        for key, value in options["expect_settings"].items():
            if environment.get(key) != value:
                raise RuntimeError(
                    f"baseline setting mismatch: {key}; establish equal load before plan"
                )
        alarms = self.alarms(options["alarms"])
        if any(a["StateValue"] != "OK" for a in alarms):
            raise RuntimeError("baseline alarms are not all OK")
        running = self.tasks(serviceName=self.target["service"])
        if not self.tasks_match(
            running,
            service["taskDefinition"],
            service["desiredCount"],
            options["container"],
        ):
            raise RuntimeError(
                "baseline tasks or immutable running image digests are not ready"
            )
        if len(self.image_digests(running, options["container"])) != 1:
            raise RuntimeError("baseline has mixed image digests")
        image = immutable_baseline_image(
            original["taskDefinition"], running, options["container"]
        )
        source_manifests = self.baseline_source_manifests(
            original["taskDefinition"], running, options["container"]
        )
        if any(t["key"] in (OWNER, PROOF) for t in original.get("tags", [])):
            raise RuntimeError("baseline task definition is owned by another demo")
        required_metrics = {
            "VitalIngestFailures",
            "PatientVitalsQueryDuration",
            "DatabaseConnections",
        }
        if not required_metrics.issubset({a.get("MetricName") for a in alarms}):
            raise RuntimeError(
                "alarms must include ingest, query latency and database connections"
            )
        for alarm in alarms:
            if alarm.get("MetricName") in {
                "VitalIngestFailures",
                "PatientVitalsQueryDuration",
            } and (
                alarm.get("Namespace") != "Healthcare/Sensor"
                or alarm.get("Dimensions")
                != [{"Name": "ServiceName", "Value": "healthcare-sensor-app"}]
            ):
                raise RuntimeError(
                    "symptom alarm does not watch the Healthcare service"
                )
            if alarm.get("MetricName") == "PatientVitalsQueryDuration" and (
                alarm.get("Statistic") != "Average"
                or alarm.get("Unit") != "Milliseconds"
                or alarm.get("ComparisonOperator") != "GreaterThanOrEqualToThreshold"
                or alarm.get("Period") != 60
                or alarm.get("EvaluationPeriods") != 2
            ):
                raise RuntimeError(
                    "query alarm must match Average/Milliseconds/60s/2-period contract"
                )
        baseline = self.metrics(
            (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()
        )
        if not self.metrics_healthy(baseline, alarms):
            raise RuntimeError("fresh successful healthy baseline metrics are required")
        if options["scenario"] == "pool-config":
            capacity = int(environment.get("DB_POOL_SIZE", "5")) + int(
                environment.get("DB_MAX_OVERFLOW", "10")
            )
            if options["pool_size"] + options["max_overflow"] >= capacity:
                raise RuntimeError(
                    "pool-config must reduce actual baseline pool capacity"
                )
        if options["image"] and options["image"].split("@")[-1] in self.image_digests(
            running, options["container"]
        ):
            raise RuntimeError("fault image equals the running healthy image")
        snapshot = {
            "target": self.target,
            "options": options,
            "service": service,
            "taskDefinition": original,
            "image": image,
            "tasks": running,
            "sourceManifests": source_manifests,
            "environment": {
                key: {"present": key in environment, "value": environment.get(key)}
                for key in SETTINGS
            },
            "alarms": alarms,
            "metrics": baseline,
        }
        self.journal.append("snapshot", snapshot)
        return {
            "planned": True,
            "awsMutated": False,
            "snapshotHash": self.journal.events[0]["hash"],
        }

    def baseline_source_manifests(self, task_definition, tasks, name):
        """Retain verified r1 startup manifests from each exact running task's log stream."""
        logging = container(task_definition, name).get("logConfiguration", {})
        options = logging.get("options", {})
        if (
            logging.get("logDriver") != "awslogs"
            or not options.get("awslogs-group")
            or not options.get("awslogs-stream-prefix")
            or options.get("awslogs-region", self.target["region"])
            != self.target["region"]
        ):
            raise RuntimeError(
                "baseline requires task-scoped awslogs source_manifest evidence in the target region"
            )
        evidence = []
        fingerprints = set()
        for task in tasks:
            stream = f"{options['awslogs-stream-prefix']}/{name}/{task['taskArn'].rsplit('/', 1)[-1]}"
            created = int(datetime.fromisoformat(task["createdAt"]).timestamp() * 1000)
            events = self.aws(
                "logs",
                "filter-log-events",
                logGroupName=options["awslogs-group"],
                logStreamNames=[stream],
                startTime=created,
                filterPattern='{ $.event = "source_manifest" }',
            ).get("events", [])
            if not events:
                raise RuntimeError(
                    "verified r1 source_manifest is missing for a running baseline task"
                )
            for event in events:
                manifest = json.loads(event["message"])
                files = manifest.get("files")
                if (
                    event.get("logStreamName") != stream
                    or event.get("timestamp", 0) < created
                    or manifest.get("event") != "source_manifest"
                    or manifest.get("revision") != "r1"
                    or manifest.get("verified") is not True
                    or not isinstance(files, dict)
                    or not files
                    or manifest.get("fingerprint")
                    != hashlib.sha256(
                        json.dumps(files, sort_keys=True).encode()
                    ).hexdigest()
                ):
                    raise RuntimeError(
                        "baseline source_manifest must verify r1 and its source fingerprint for the running task"
                    )
                fingerprints.add(manifest["fingerprint"])
            evidence.append(
                {
                    "taskArn": task["taskArn"],
                    "imageDigest": self.image_digests([task], name)[0],
                    "logGroupName": options["awslogs-group"],
                    "logStreamName": stream,
                    "events": events,
                }
            )
        if len(fingerprints) != 1:
            raise RuntimeError(
                "baseline tasks report inconsistent r1 source fingerprints"
            )
        return evidence

    @staticmethod
    def image_digests(tasks, name):
        """Extract all application digests without confusing sidecar images."""
        return sorted(
            {
                c.get("imageDigest")
                for task in tasks
                for c in task.get("containers", [])
                if c["name"] == name and c.get("imageDigest")
            }
        )

    def tasks_match(self, tasks, arn, count, name=None):
        """Verify running application tasks belong to the expected revision."""
        name = name or self.journal.snapshot["options"]["container"]
        return (
            len(tasks) == count
            and bool(tasks)
            and all(
                t.get("lastStatus") == "RUNNING"
                and t["taskDefinitionArn"] == arn
                and self.image_digests([t], name)
                for t in tasks
            )
        )

    def owner_tags(self):
        """Bind cloud ownership to both caller RunId and immutable snapshot hash."""
        return {
            OWNER: self.journal.snapshot["options"]["run_id"],
            PROOF: self.journal.events[0]["hash"],
        }

    def assert_owner(self, service, allow_unclaimed=False):
        """Reject foreign ownership and drift rather than overwriting others' work."""
        snapshot = self.journal.snapshot
        if snapshot["target"] != self.target:
            raise RuntimeError("AWS target/account/region mismatch")
        tags = {t["key"]: t["value"] for t in service.get("tags", [])}
        ours = all(tags.get(k) == v for k, v in self.owner_tags().items())
        unclaimed = OWNER not in tags and PROOF not in tags
        if not ours and not (allow_unclaimed and unclaimed):
            raise RuntimeError("ownership mismatch; refusing service mutation")
        allowed = {snapshot["service"]["taskDefinition"]}
        allowed.update(e["data"]["arn"] for e in self.journal.find("service_revision"))
        if (
            settings(service) != settings(snapshot["service"])
            or service["taskDefinition"] not in allowed
            or any(
                d["taskDefinition"] not in allowed
                for d in service.get("deployments", [])
            )
        ):
            raise RuntimeError(
                "dirty service settings or foreign deployment; refusing overwrite"
            )
        if (
            not ours
            and service["taskDefinition"] != snapshot["service"]["taskDefinition"]
        ):
            raise RuntimeError("unclaimed changed revision; refusing overwrite")

    def register(self, maintenance=False):
        """Clone the original definition; tag and record the new ARN before use."""
        snapshot = self.journal.snapshot
        options = snapshot["options"]
        td = copy.deepcopy(snapshot["taskDefinition"]["taskDefinition"])
        for key in READ_ONLY_TD:
            td.pop(key, None)
        app = container(td, options["container"])
        environment = env_map(td, options["container"])
        environment["RCA_TEST_RUN_ID"] = options["run_id"]
        if maintenance:
            command = maintenance_command(options)
            # A one-shot job must not inherit the web health check or HTTP sidecars.
            td["family"] = (
                f"{td['family'][:210]}-demo-maint-{self.journal.events[0]['hash'][:12]}"
            )
            td["containerDefinitions"] = [app]
            app.pop("healthCheck", None)
            app.pop("dependsOn", None)
            app.pop("portMappings", None)
            app["entryPoint"] = command[:1]
            app["command"] = command[1:]
            app["image"] = (
                image_repository(snapshot["image"])
                + "@"
                + self.image_digests(snapshot["tasks"], options["container"])[0]
            )
            environment["TRAFFIC_ENABLED"] = "false"
            environment["DB_OBSERVABILITY_ENABLED"] = "false"
        elif options["scenario"] == "pool-config":
            environment.update(
                DB_POOL_SIZE=str(options["pool_size"]),
                DB_MAX_OVERFLOW=str(options["max_overflow"]),
                DB_POOL_TIMEOUT_SECONDS=str(options["pool_timeout"]),
            )
        else:
            app["image"] = options["image"]
            environment["DEPLOYED_REVISION"] = options["revision"]
        app["environment"] = [
            {"name": key, "value": value} for key, value in sorted(environment.items())
        ]
        if not maintenance:
            self.assert_scenario_environment(td)
        original_tags = {
            t["key"]: t["value"]
            for t in snapshot["taskDefinition"].get("tags", [])
            if not t["key"].startswith("aws:")
        }
        td["tags"] = [
            {"key": key, "value": value}
            for key, value in (original_tags | self.owner_tags()).items()
        ]
        kind = "maintenance_revision" if maintenance else "service_revision"
        self.record("register_intent", {"kind": kind, "definition": td})
        registered = self.aws("ecs", "register-task-definition", **td)
        arn = registered["taskDefinition"]["taskDefinitionArn"]
        self.record(kind, {"arn": arn, "response": registered})
        return arn

    def assert_scenario_environment(self, candidate):
        """Reject any service environment change outside the selected cause/lineage."""
        snapshot = self.journal.snapshot
        options = snapshot["options"]
        original = env_map(
            snapshot["taskDefinition"]["taskDefinition"], options["container"]
        )
        proposed = env_map(candidate, options["container"])
        permitted = {"RCA_TEST_RUN_ID"}
        if options["scenario"] == "pool-config":
            permitted.update(
                {"DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT_SECONDS"}
            )
        else:
            permitted.add("DEPLOYED_REVISION")
        changed = {
            key
            for key in original.keys() | proposed.keys()
            if (key in original, original.get(key))
            != (key in proposed, proposed.get(key))
        }
        if changed - permitted:
            raise RuntimeError(
                "same-load invariant violated; unexpected environment changes: "
                + ", ".join(sorted(changed - permitted))
            )

    def apply(self, wait_seconds=0):
        """Require durable new intents; attempt cleanup even when failure logging fails."""
        if self.journal.find("apply_intent") or self.journal.find("restore_intent"):
            raise RuntimeError("apply is single-use; inspect or restore this RunId")
        snapshot = self.journal.snapshot
        if snapshot["options"]["scenario"] == "maintenance-lock":
            maintenance_command(snapshot["options"])
        immutable_baseline_image(
            snapshot["taskDefinition"]["taskDefinition"],
            snapshot["tasks"],
            snapshot["options"]["container"],
        )
        if not snapshot.get("sourceManifests"):
            raise RuntimeError(
                "snapshot lacks verified r1 baseline source evidence; create a new plan"
            )
        service = self.service()
        self.assert_owner(service, allow_unclaimed=True)
        if not stable(service, self.journal.snapshot["service"]["taskDefinition"]):
            raise RuntimeError("baseline rollout changed since plan")
        fresh = self.status()
        if not all(
            fresh["checks"][key]
            for key in (
                "ownershipMatches",
                "originalSettings",
                "originalRollout",
                "originalTasks",
                "originalImageDigests",
                "alarmsOk",
                "alarmSettingsUnchanged",
            )
        ):
            raise RuntimeError("baseline changed since plan")
        baseline = self.metrics(
            (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()
        )
        if not self.metrics_healthy(baseline, fresh["alarms"]):
            raise RuntimeError("healthy baseline no longer observed")
        self.record("apply_baseline", baseline)
        self.record("apply_intent", {})
        try:
            self.aws(
                "ecs",
                "tag-resource",
                resourceArn=service["serviceArn"],
                tags=[{"key": k, "value": v} for k, v in self.owner_tags().items()],
            )
            self.assert_owner(self.service())
            maintenance = (
                self.journal.snapshot["options"]["scenario"] == "maintenance-lock"
            )
            arn = self.register(maintenance)
            self.assert_owner(self.service())
            if maintenance:
                original = self.journal.snapshot["service"]
                request = {
                    "cluster": self.target["cluster"],
                    "taskDefinition": arn,
                    "count": 1,
                    "startedBy": self.token(),
                    "clientToken": self.token(),
                    "networkConfiguration": original["networkConfiguration"],
                    "tags": [
                        {"key": k, "value": v} for k, v in self.owner_tags().items()
                    ],
                }
                if original.get("capacityProviderStrategy"):
                    request["capacityProviderStrategy"] = original[
                        "capacityProviderStrategy"
                    ]
                else:
                    request["launchType"] = original.get("launchType", "FARGATE")
                if original.get("platformVersion"):
                    request["platformVersion"] = original["platformVersion"]
                self.record("run_task_intent", request)
                response = self.aws("ecs", "run-task", **request)
                self.record("maintenance_tasks", response)
                if len(response.get("tasks", [])) != 1:
                    raise RuntimeError("maintenance task launch was not acknowledged")
            else:
                self.record("update_intent", {"arn": arn})
                response = self.aws(
                    "ecs",
                    "update-service",
                    cluster=self.target["cluster"],
                    service=self.target["service"],
                    taskDefinition=arn,
                )
                self.record("update_response", response)
            return self.wait_applied(wait_seconds)
        except BaseException as error:
            self.record("apply_error", {"error": str(error)}, recovery=True)
            try:
                self.restore(wait_seconds=wait_seconds)
            except RECOVERABLE_ERRORS as cleanup_error:
                self.record(
                    "cleanup_error", {"error": str(cleanup_error)}, recovery=True
                )
            raise

    def wait_applied(self, seconds):
        """Observe a bounded rollout, restoring through apply's error handler."""
        deadline = time.monotonic() + seconds
        revisions = self.journal.find("service_revision")
        expected = (
            revisions[-1]["data"]["arn"]
            if revisions
            else self.journal.snapshot["service"]["taskDefinition"]
        )
        while True:
            result = self.status()
            if result["ownershipError"]:
                raise RuntimeError(result["ownershipError"])
            revision = result["revisionObservation"]
            if revision["state"] == "original-after-apply" and (
                revision["faultRevisionPreviouslyObserved"]
                or revision["rollbackEventEvidence"]
            ):
                raise RuntimeError(
                    "service returned to original during apply; inspect rollback evidence"
                )
            if any(
                d.get("taskDefinition") == expected
                and d.get("rolloutState") == "FAILED"
                for d in result["service"].get("deployments", [])
            ):
                raise RuntimeError("scenario deployment failed")
            jobs = result["maintenanceTasks"]
            if jobs and any(t["lastStatus"] == "STOPPED" for t in jobs):
                raise RuntimeError("maintenance task exited before running observation")
            rollout_ready = stable(result["service"], expected) and self.tasks_match(
                result["tasks"],
                expected,
                self.journal.snapshot["service"]["desiredCount"],
            )
            if not seconds or (
                rollout_ready
                and (bool(revisions) or any(t["lastStatus"] == "RUNNING" for t in jobs))
            ):
                result["applyRolloutObserved"] = rollout_ready
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError("scenario rollout observation timed out")
            time.sleep(min(10, max(0, deadline - time.monotonic())))

    def token(self):
        """Derive a deterministic ECS idempotency/start marker for task discovery."""
        return "demo-" + self.journal.events[0]["hash"][:32]

    def owned_tasks(self, *, recovery=False):
        """Validate rediscovered jobs; recovery logging cannot hide proven owned tasks."""
        if not self.journal.find("run_task_intent"):
            return []
        discovered = self.tasks(startedBy=self.token())
        known = {
            task["taskArn"]
            for event in self.journal.find("maintenance_tasks")
            for task in event["data"].get("tasks", [])
        }
        known.update(
            task["taskArn"]
            for event in self.journal.find("owned_tasks")
            for task in event["data"]
        )
        known.update(self.discovered_task_arns)
        known.difference_update(task["taskArn"] for task in discovered)
        discovered.extend(self.describe_tasks(sorted(known)))
        arns = {e["data"]["arn"] for e in self.journal.find("maintenance_revision")}
        for task in discovered:
            tags = {t["key"]: t["value"] for t in task.get("tags", [])}
            if (
                task.get("startedBy") != self.token()
                or task["taskDefinitionArn"] not in arns
                or any(tags.get(k) != v for k, v in self.owner_tags().items())
            ):
                raise RuntimeError("maintenance task ownership mismatch; refusing stop")
        self.discovered_task_arns.update(task["taskArn"] for task in discovered)
        self.record("owned_tasks", discovered, recovery=recovery)
        return discovered

    def metrics(self, start):
        """Read complete post-transition minute buckets; absent data never passes."""
        start_time = datetime.fromisoformat(start)
        aligned = datetime.fromtimestamp(
            math.ceil(start_time.timestamp() / 60) * 60, timezone.utc
        )
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Use the last two full minutes once enough post-transition data exists.
        aligned = max(aligned, end - timedelta(minutes=2))
        if end - aligned < timedelta(minutes=2):
            return {
                "ready": False,
                "start": aligned.isoformat(),
                "end": end.isoformat(),
            }
        values = {}
        for metric, unit in [
            ("VitalIngestAttempts", "Count"),
            ("VitalIngestFailures", "Count"),
            ("PatientVitalsQueryDuration", "Milliseconds"),
        ]:
            values[metric] = self.aws(
                "cloudwatch",
                "get-metric-statistics",
                Namespace="Healthcare/Sensor",
                MetricName=metric,
                Dimensions=[{"Name": "ServiceName", "Value": "healthcare-sensor-app"}],
                Unit=unit,
                StartTime=aligned.isoformat(),
                EndTime=end.isoformat(),
                Period=60,
                Statistics=["Sum", "Average", "SampleCount"],
            )
        return {
            "ready": True,
            "start": aligned.isoformat(),
            "end": end.isoformat(),
            "values": values,
        }

    @staticmethod
    def metrics_healthy(metrics, alarms):
        """Require successful ingests and actual non-breaching query observations."""
        if not metrics.get("ready"):
            return False
        values = metrics["values"]
        attempts = values["VitalIngestAttempts"].get("Datapoints", [])
        failures = values["VitalIngestFailures"].get("Datapoints", [])
        queries = values["PatientVitalsQueryDuration"].get("Datapoints", [])
        threshold = next(
            (
                a.get("Threshold")
                for a in alarms
                if a.get("MetricName") == "PatientVitalsQueryDuration"
            ),
            None,
        )
        start = datetime.fromisoformat(metrics["start"])
        expected_times = {start, start + timedelta(minutes=1)}
        aligned = all(
            {datetime.fromisoformat(p["Timestamp"]) for p in points} == expected_times
            for points in (attempts, failures, queries)
        )
        return (
            threshold is not None
            and len(attempts) == len(failures) == len(queries) == 2
            and aligned
            and all(p["Sum"] > 0 for p in attempts)
            and all(p["Sum"] == 0 for p in failures)
            and all(p["SampleCount"] > 0 and p["Average"] < threshold for p in queries)
        )

    def status(self, *, recovery=False):
        """Observe recovery conservatively, returning logging failures during cleanup."""
        snapshot = self.journal.snapshot
        service = self.service()
        ownership_error = None
        try:
            self.assert_owner(
                service,
                allow_unclaimed=bool(self.journal.find("restore_intent"))
                or not self.journal.find("apply_intent"),
            )
        except RuntimeError as error:
            ownership_error = str(error)
        tasks = self.tasks(serviceName=self.target["service"])
        jobs = self.owned_tasks(recovery=recovery)
        alarms = self.alarms(snapshot["options"]["alarms"])
        restore = self.journal.find("restore_intent")
        original = snapshot["service"]["taskDefinition"]
        original_tasks = self.tasks_match(
            tasks, original, snapshot["service"]["desiredCount"]
        )
        digest_match = self.image_digests(
            tasks, snapshot["options"]["container"]
        ) == self.image_digests(snapshot["tasks"], snapshot["options"]["container"])
        # A lost RunTask response with no discovered task remains unresolved.
        jobs_stopped = (not self.journal.find("run_task_intent") or bool(jobs)) and all(
            t["lastStatus"] == "STOPPED" for t in jobs
        )
        configuration_restored = (
            ownership_error is None
            and stable(service, original)
            and original_tasks
            and digest_match
            and jobs_stopped
        )
        observed = self.journal.find("restored_rollout_observed")
        if restore and configuration_restored and not observed:
            event = self.record("restored_rollout_observed", {}, recovery=recovery)
            observed = [event] if event else []
        since = (
            observed[0]["at"]
            if observed
            else (restore[0]["at"] if restore else self.journal.events[0]["at"])
        )
        metrics = self.metrics(since)
        original_alarms = {
            a["AlarmName"]: {k: a.get(k) for k in ALARM_SETTINGS}
            for a in snapshot["alarms"]
        }
        live_alarms = {
            a["AlarmName"]: {k: a.get(k) for k in ALARM_SETTINGS} for a in alarms
        }
        checks = {
            "ownershipMatches": ownership_error is None,
            "originalSettings": settings(service) == settings(snapshot["service"]),
            "originalRollout": stable(service, original),
            "originalTasks": original_tasks,
            "originalImageDigests": digest_match,
            "ownedMaintenanceStopped": jobs_stopped,
            "alarmsOk": all(a["StateValue"] == "OK" for a in alarms),
            "alarmSettingsUnchanged": original_alarms == live_alarms,
            "successfulFreshSymptoms": self.metrics_healthy(metrics, alarms),
        }
        query_alarm = next(
            a for a in alarms if a.get("MetricName") == "PatientVitalsQueryDuration"
        )
        result = {
            "runId": snapshot["options"]["run_id"],
            "service": service,
            "tasks": tasks,
            "maintenanceTasks": jobs,
            "maintenanceRestoration": self.maintenance_restoration(jobs),
            "alarms": alarms,
            "metrics": metrics,
            "checks": checks,
            "queryLatency": {
                "statistic": query_alarm["Statistic"],
                "thresholdMs": query_alarm["Threshold"],
                "unit": query_alarm["Unit"],
                "periodSeconds": query_alarm["Period"],
            },
            "ownershipError": ownership_error,
            "revisionObservation": self.revision_observation(service),
            "recoveryVerified": bool(restore) and all(checks.values()),
            "scenarioSuccess": False,
            "note": "Operational observations only; causal reproduction/model evaluation require separate evidence.",
        }
        self.evidence_result(result)
        self.record("status", result, recovery=recovery)
        return self.evidence_result(result)

    def revision_observation(self, service):
        """Distinguish original/owned/foreign revisions without inventing rollback cause."""
        original = self.journal.snapshot["service"]["taskDefinition"]
        owned = {e["data"]["arn"] for e in self.journal.find("service_revision")}
        current = service["taskDefinition"]
        restore = self.journal.find("restore_intent")
        apply = self.journal.find("apply_intent")
        if current not in {original, *owned} or any(
            deployment["taskDefinition"] not in {original, *owned}
            for deployment in service.get("deployments", [])
        ):
            state = "foreign-revision"
        elif current in owned:
            state = "owned-fault-revision"
        elif restore:
            state = "original-after-restore"
        elif apply and owned:
            state = "original-after-apply"
        else:
            state = "original"
        previously_observed = any(
            event["data"].get("service", {}).get("taskDefinition") in owned
            for event in self.journal.find("status")
        )
        rollback_events = []
        for event in service.get("events", []):
            created = event.get("createdAt")
            if (
                apply
                and created
                and datetime.fromisoformat(created)
                >= datetime.fromisoformat(apply[0]["at"])
                and re.search(
                    r"roll(?:ed|ing)?\s*back", event.get("message", ""), re.IGNORECASE
                )
            ):
                rollback_events.append(event)
        before_restore = self.journal.find("restore_service_observation")
        return {
            "state": state,
            "currentTaskDefinition": current,
            "originalTaskDefinition": original,
            "faultRevisionPreviouslyObserved": previously_observed,
            "rollbackEventEvidence": rollback_events,
            "alreadyOriginalAtRestore": (
                before_restore[0]["data"]["taskDefinition"] == original
                if before_restore
                else None
            ),
            "returnCause": (
                "ecs-rollback-event"
                if current == original and rollback_events
                else "not-established"
            ),
        }

    def maintenance_restoration(self, jobs):
        """Separate observed termination from a claim that approved restore caused it."""
        before = self.journal.find("maintenance_before_restore")
        stopped_before = [
            task["taskArn"]
            for event in before
            for task in event["data"]
            if task.get("lastStatus") == "STOPPED"
        ]
        requested = [e["data"]["taskArn"] for e in self.journal.find("stop_intent")]
        return {
            "stoppedBeforeRestore": sorted(set(stopped_before)),
            "stopRequestedTasks": sorted(set(requested)),
            "exitEvidence": [
                {
                    key: task.get(key)
                    for key in (
                        "taskArn",
                        "lastStatus",
                        "desiredStatus",
                        "startedAt",
                        "stoppingAt",
                        "stoppedAt",
                        "stopCode",
                        "stoppedReason",
                        "containers",
                    )
                }
                for task in jobs
            ],
            "approvedRestorationCausedRecovery": False,
            "causality": "not-established",
            "note": "An expired or already-stopped hold is not evidence of an approved restore. A stop request alone also does not prove lock release causality.",
        }

    def restore(self, wait_seconds=0):
        """Persist cleanup evidence before release; keep journal failures unresolved."""
        started = time.monotonic()
        deadline = started + wait_seconds
        result = self.restore_once()
        while not result["recoveryVerified"] and time.monotonic() < deadline:
            time.sleep(min(10, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            result = self.restore_once()
        result["restorationState"] = (
            "verified" if result["recoveryVerified"] else "pending"
        )
        result["waitSeconds"] = wait_seconds
        result["waitElapsedSeconds"] = time.monotonic() - started
        result["waitExpired"] = (
            bool(wait_seconds)
            and not result["recoveryVerified"]
            and time.monotonic() >= deadline
        )
        self.record("restore_wait_result", result, recovery=True)
        self.evidence_result(result)
        if result["recoveryVerified"]:
            self.release_owner(result)
        self.cleanup_result = self.evidence_result(result)
        return result

    def release_owner(self, result):
        """Release only with durable recovery evidence; reclaim after a lost release log."""
        untagged = False
        try:
            service = self.service()
            self.assert_owner(service, allow_unclaimed=True)
            tags = {t["key"]: t["value"] for t in service.get("tags", [])}
            if tags.get(OWNER) == self.owner_tags()[OWNER]:
                self.record("release_intent", {})
                self.aws(
                    "ecs",
                    "untag-resource",
                    resourceArn=service["serviceArn"],
                    tagKeys=[OWNER, PROOF],
                )
                untagged = True
            self.record("released", {})
        except RECOVERABLE_ERRORS as error:
            result["errors"].append({"step": "release", "error": str(error)})
            result["recoveryVerified"] = False
            result["restorationState"] = "pending"
            if untagged:
                try:
                    service = self.service()
                    self.assert_owner(service, allow_unclaimed=True)
                    # Reclaim only our own completed release, never a peer's tags.
                    tags = {t["key"]: t["value"] for t in service.get("tags", [])}
                    if OWNER not in tags and PROOF not in tags:
                        self.aws(
                            "ecs",
                            "tag-resource",
                            resourceArn=service["serviceArn"],
                            tags=[
                                {"key": key, "value": value}
                                for key, value in self.owner_tags().items()
                            ],
                        )
                except RECOVERABLE_ERRORS as reclaim_error:
                    result["errors"].append(
                        {"step": "reclaim-owner", "error": str(reclaim_error)}
                    )

    def restore_once(self):
        """Use persisted ownership/intents for independent cleanup despite logging loss."""
        if not self.journal.find("restore_intent"):
            self.record("restore_intent", {}, recovery=True)
        errors = []
        try:
            service = self.service()
            if not self.journal.find("restore_service_observation"):
                self.record("restore_service_observation", service, recovery=True)
            self.assert_owner(service, allow_unclaimed=True)
            original = self.journal.snapshot["service"]["taskDefinition"]
            if service["taskDefinition"] != original:
                snapshot = self.journal.snapshot
                immutable_baseline_image(
                    snapshot["taskDefinition"]["taskDefinition"],
                    snapshot["tasks"],
                    snapshot["options"]["container"],
                )
                intent = self.record(
                    "restore_update_intent", {"arn": original}, recovery=True
                )
                if intent is None and not any(
                    event["data"]["arn"] == service["taskDefinition"]
                    for event in self.journal.find("update_intent")
                ):
                    raise RuntimeError(
                        "no persisted update intent; refusing unjournaled restore"
                    )
                response = self.aws(
                    "ecs",
                    "update-service",
                    cluster=self.target["cluster"],
                    service=self.target["service"],
                    taskDefinition=original,
                )
                self.record("restore_update_response", response, recovery=True)
        except RECOVERABLE_ERRORS as error:
            errors.append({"step": "service", "error": str(error)})
        try:
            jobs = self.owned_tasks(recovery=True)
            if not self.journal.find("maintenance_before_restore"):
                self.record("maintenance_before_restore", jobs, recovery=True)
            for task in jobs:
                if task["lastStatus"] != "STOPPED":
                    try:
                        self.record(
                            "stop_intent", {"taskArn": task["taskArn"]}, recovery=True
                        )
                        response = self.aws(
                            "ecs",
                            "stop-task",
                            cluster=self.target["cluster"],
                            task=task["taskArn"],
                            reason=f"Restore owned demo {self.journal.snapshot['options']['run_id']}",
                        )
                        self.record("stop_response", response, recovery=True)
                    except RECOVERABLE_ERRORS as error:
                        errors.append({"step": "maintenance", "error": str(error)})
        except RECOVERABLE_ERRORS as error:
            errors.append({"step": "maintenance-discovery", "error": str(error)})
        try:
            result = self.status(recovery=True)
        except RECOVERABLE_ERRORS as error:
            errors.append({"step": "verification", "error": str(error)})
            result = {"recoveryVerified": False}
        result["errors"] = errors
        if errors:
            result["recoveryVerified"] = False
        self.evidence_result(result)
        self.record("restore_result", result, recovery=True)
        return self.evidence_result(result)


def parse_args(argv=None):
    """Validate immutable scenario controls before contacting AWS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "apply", "status", "restore"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--journal-root", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        choices=[
            "pool-config",
            "query-revision",
            "session-revision",
            "maintenance-lock",
        ],
    )
    parser.add_argument("--container", default="healthcare")
    parser.add_argument(
        "--image", help="Caller-supplied repository@sha256:<64 hex> fault image"
    )
    parser.add_argument(
        "--revision", help="Source/build revision recorded in DEPLOYED_REVISION"
    )
    parser.add_argument("--pool-size", type=int, default=1)
    parser.add_argument("--max-overflow", type=int, default=0)
    parser.add_argument("--pool-timeout", type=float, default=1.0)
    parser.add_argument("--hold-seconds", type=float, default=7200)
    parser.add_argument("--maintenance-schema", default="public")
    parser.add_argument(
        "--expect-setting",
        action="append",
        default=[],
        help="KEY=VALUE already present in healthy baseline; never changes traffic during a scenario",
    )
    parser.add_argument(
        "--alarm",
        action="append",
        default=[],
        help="Required alarm name; repeat for ingest, query latency and connections",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="Bounded polling for restore verification or apply rollout, 0 inspects once",
    )
    arguments = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(arguments)
    plan_controls = {
        "--scenario",
        "--container",
        "--image",
        "--revision",
        "--pool-size",
        "--max-overflow",
        "--pool-timeout",
        "--hold-seconds",
        "--maintenance-schema",
        "--expect-setting",
        "--alarm",
    }
    if args.action != "plan" and plan_controls.intersection(
        argument.split("=", 1)[0] for argument in arguments
    ):
        parser.error(
            "scenario and baseline controls are frozen by plan; apply/status/restore cannot override them"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", args.run_id):
        parser.error("RunId must be 1..48 letters, digits, underscores or hyphens")
    if (
        args.pool_size < 1
        or args.max_overflow < 0
        or not math.isfinite(args.pool_timeout)
        or args.pool_timeout <= 0
    ):
        parser.error("pool size/timeout must be positive and overflow nonnegative")
    if (
        not math.isfinite(args.hold_seconds)
        or not 0 < args.hold_seconds <= 7200
        or args.wait_seconds < 0
    ):
        parser.error(
            "hold-seconds must be finite, positive and <=7200; wait-seconds nonnegative"
        )
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", args.maintenance_schema):
        parser.error("maintenance-schema must be a safe PostgreSQL identifier")
    args.expect_settings = {}
    for entry in args.expect_setting:
        key, separator, value = entry.partition("=")
        if not separator or key not in SETTINGS:
            parser.error("--expect-setting requires a controlled KEY=VALUE")
        args.expect_settings[key] = value
    if args.action == "plan":
        if not args.scenario or len(set(args.alarm)) < 3:
            parser.error(
                "plan requires --scenario and --alarm for ingest, query latency and connections"
            )
        revision = args.scenario in ("query-revision", "session-revision")
        if revision and (
            not args.image
            or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", args.image)
            or not args.revision
        ):
            parser.error(
                "revision scenarios require immutable --image repository@sha256:digest and --revision"
            )
        if not revision and args.image:
            parser.error("--image is only valid for revision scenarios")
    return args


def interrupted(signum, frame):
    """Convert termination into normal failure cleanup when Python can handle it."""
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv=None):
    """Run one command under a nonblocking service lock and print JSON evidence."""
    args = parse_args(argv)
    aws = Aws(args.region, args.profile)
    account = aws("sts", "get-caller-identity")["Account"]
    target = {
        "account": account,
        "region": args.region,
        "cluster": args.cluster,
        "service": args.service,
    }
    # Canonical cloud ARNs prevent name/ARN aliases from bypassing the local lock.
    described = aws(
        "ecs", "describe-services", cluster=args.cluster, services=[args.service]
    )["services"]
    if len(described) != 1:
        raise RuntimeError("target service missing")
    target.update(
        cluster=described[0]["clusterArn"], service=described[0]["serviceArn"]
    )
    directory = args.journal_root.resolve() / digest(target)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another demo command owns this service lock") from error
        journal = Journal(directory / args.run_id)
        demo = Demo(aws, journal, target)
        if args.action == "plan":
            for path in directory.iterdir():
                if path.is_dir() and path != journal.path:
                    previous = Journal(path)
                    if previous.events and not previous.find("released"):
                        raise RuntimeError(
                            f"unrestored local RunId {path.name}; refusing plan"
                        )
            options = {
                key: getattr(args, key)
                for key in [
                    "run_id",
                    "scenario",
                    "container",
                    "image",
                    "revision",
                    "pool_size",
                    "max_overflow",
                    "pool_timeout",
                    "hold_seconds",
                    "maintenance_schema",
                    "expect_settings",
                ]
            }
            options["alarms"] = args.alarm
            result = demo.plan(options)
        else:
            if journal.snapshot["target"] != target:
                raise RuntimeError("journal target mismatch")
            if args.action == "apply":
                try:
                    result = demo.apply(args.wait_seconds)
                except RECOVERABLE_ERRORS as error:
                    cleanup = demo.cleanup_result
                    if not cleanup:
                        raise
                    print(
                        json.dumps(
                            {
                                "error": str(error),
                                "operationSucceeded": False,
                                "recoveryVerified": cleanup["recoveryVerified"],
                                "cleanup": cleanup,
                                "journal": str(journal.path),
                            },
                            indent=2,
                            default=str,
                        ),
                        file=sys.stderr,
                    )
                    return 1
            elif args.action == "restore":
                result = demo.restore(args.wait_seconds)
            else:
                result = demo.status()
            deadline = time.monotonic() + args.wait_seconds
            while (
                args.action == "status"
                and args.wait_seconds
                and time.monotonic() < deadline
                and not result.get("recoveryVerified")
            ):
                time.sleep(min(10, max(0, deadline - time.monotonic())))
                result = demo.status()
        result["journal"] = str(journal.path)
        print(json.dumps(result, indent=2, default=str))
        return (
            2 if args.action == "restore" and not result.get("recoveryVerified") else 0
        )


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except RECOVERABLE_ERRORS as error:
        print(
            json.dumps({"error": str(error), "recoveryVerified": False}),
            file=sys.stderr,
        )
        sys.exit(1)
