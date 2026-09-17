#!/usr/bin/env python3
"""Plan, apply, inspect and restore owned Healthcare demo changes.

AWS CLI v2 and the project's Agent Python environment are required. Planning only reads AWS. All commands
retain hash-linked, create-only evidence under --journal-root (use the SAME
shared directory for every operator). See run_realistic_demo.html for examples.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import gzip
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

import tomllib

OWNER = "RealisticDemoRunId"
PROOF = "RealisticDemoJournal"
JOURNAL_VERSION = 2
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
    "InsufficientDataActions",
    "EvaluateLowSampleCountPercentile",
    "ThresholdMetricId",
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


def canonical(value):
    """Match the agent reader's UTF-8 wire format without changing legacy journal hashes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value):
    """Hash a canonical JSON value for journal integrity and ownership."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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


_AGENT_COMMAND = "uv run --project packages/agent --no-sync python scripts/run_realistic_demo.py <same arguments>"


def _locked_botocore_version():
    """Read the admitted Agent dependency version without resolving or installing anything."""
    lock = Path(__file__).resolve().parents[1] / "packages/agent/uv.lock"
    packages = tomllib.loads(lock.read_text())["package"]
    matches = [item["version"] for item in packages if item.get("name") == "botocore"]
    if len(matches) != 1:
        raise RuntimeError(
            "Agent lock has no unique botocore version; " + _AGENT_COMMAND
        )
    return matches[0]


def _installed_ecs_model():
    """Inspect the interpreter's actual ECS model bytes, never infer compatibility from known fields."""
    try:
        import botocore
    except ImportError:
        raise RuntimeError("Agent botocore is required; " + _AGENT_COMMAND) from None
    source = Path(botocore.__file__).parent / "data/ecs/2014-11-13"
    plain, compressed = source / "service-2.json", source / "service-2.json.gz"
    raw = (
        plain.read_bytes()
        if plain.exists()
        else gzip.decompress(compressed.read_bytes())
    )
    if json.loads(raw).get("metadata", {}).get("endpointPrefix") != "ecs":
        raise RuntimeError("Installed service model is not ECS; " + _AGENT_COMMAND)
    return botocore.__version__, raw


def _agent_ecs_model_identity():
    """Use the local Agent environment as the independent locked-model reference, with no AWS calls."""
    root = Path(__file__).resolve().parents[1]
    python = root / "packages/agent/.venv/bin/python"
    if not python.is_file():
        raise RuntimeError(
            "Agent environment is missing; prepare it from uv.lock, then run "
            + _AGENT_COMMAND
        )
    code = """import gzip,hashlib,json; from pathlib import Path; import botocore
p=Path(botocore.__file__).parent/'data/ecs/2014-11-13'
f=p/'service-2.json'
b=f.read_bytes() if f.exists() else gzip.decompress((p/'service-2.json.gz').read_bytes())
print(json.dumps({'version':botocore.__version__,'sha256':hashlib.sha256(b).hexdigest()}))
"""
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", code],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        identity = json.loads(result.stdout)
        if not isinstance(identity, dict) or set(identity) != {"version", "sha256"}:
            raise ValueError("invalid Agent model identity")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Cannot verify locked Agent ECS model; " + _AGENT_COMMAND
        ) from exc
    return identity


class Aws:
    """Small injectable AWS CLI adapter; command arguments never use a shell."""

    def __init__(self, region, profile=None):
        """Pin every command to the caller's region and optional profile."""
        self.context = ["--region", region]
        if profile:
            self.context += ["--profile", profile]
        self._model_directory = None
        self._model_identity = None

    def preflight(self):
        """Reject interpreter/model drift before any AWS command or journal mutation."""
        if self._model_identity is not None:
            return dict(self._model_identity)
        expected = _locked_botocore_version()
        version, raw = _installed_ecs_model()
        if version != expected:
            raise RuntimeError(
                f"ECS model mismatch: interpreter botocore {version}, Agent lock {expected}. "
                + _AGENT_COMMAND
            )
        reference = _agent_ecs_model_identity()
        digest = hashlib.sha256(raw).hexdigest()
        if reference.get("version") != expected or reference.get("sha256") != digest:
            raise RuntimeError(
                "ECS model bytes differ from the locked Agent environment. "
                + _AGENT_COMMAND
            )
        self._model_directory = tempfile.TemporaryDirectory(prefix="rca-ecs-model-")
        destination = Path(self._model_directory.name) / "ecs/2014-11-13"
        destination.mkdir(parents=True)
        (destination / "service-2.json").write_bytes(raw)
        self._model_identity = {
            "python": sys.executable,
            "botocore_version": version,
            "ecs_model_sha256": digest,
        }
        return dict(self._model_identity)

    def _environment(self, service):
        """Give CLI ECS reads the same full response shape as the locked Python SDK.

        Older CLI loaders do not read compressed custom service models. Materialize
        the locked model as plain JSON instead of silently dropping mutable fields.
        Only this child process's ECS model path changes; the installed CLI does not.
        """
        environment = os.environ.copy()
        if service != "ecs":
            return environment
        self.preflight()
        existing = environment.get("AWS_DATA_PATH")
        environment["AWS_DATA_PATH"] = self._model_directory.name + (
            os.pathsep + existing if existing else ""
        )
        return environment

    def __call__(self, service, operation, /, **payload):
        """Execute one CLI operation and reject transport or partial failures."""
        output_file = payload.pop("OutputFile", None)
        # --cli-input-json treats blob values as base64, not filesystem paths.
        # The S3 CLI's --body option owns file loading; preserve the actual bytes.
        body_file = (
            payload.pop("Body", None)
            if (service, operation) == ("s3api", "put-object")
            else None
        )
        object_args = []
        if service == "s3api" and operation in {"get-object", "put-object"}:
            # S3's file-transfer customization validates these CLI arguments
            # before applying the JSON payload (notably for GetObject).
            object_args = [
                "--bucket",
                payload.pop("Bucket"),
                "--key",
                payload.pop("Key"),
            ]
        json_args = ["--cli-input-json", json.dumps(payload)]
        if (service, operation) == ("s3api", "get-object"):
            flags = {
                "ExpectedBucketOwner": "--expected-bucket-owner",
                "VersionId": "--version-id",
            }
            if set(payload) - flags.keys():
                raise ValueError("unsupported GetObject CLI option")
            object_args.extend(
                part for key, value in payload.items() for part in (flags[key], value)
            )
            json_args = []
        try:
            process = subprocess.run(
                [
                    "aws",
                    *self.context,
                    service,
                    operation,
                    *json_args,
                    "--output",
                    "json",
                    "--no-cli-pager",
                    *object_args,
                    *(["--body", body_file] if body_file is not None else []),
                    *([output_file] if output_file else []),
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
                env=self._environment(service),
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


def validate_declared_source_locations(message):
    """Preserve optional byte-bound locators without claiming Git contents were read."""
    if "source_locations" not in message:
        return
    locations = message["source_locations"]
    location = (
        locations.get("revision/write.py") if isinstance(locations, dict) else None
    )
    files = message.get("files")
    paths = {
        "v1": "packages/healthcare-sensor-app/src/test_service/revision/write.py",
        "v2": "packages/healthcare-sensor-app/demo/revisions/v2/revision/write.py",
    }
    if (
        message.get("event") != "source_manifest"
        or not isinstance(message.get("revision"), str)
        or message.get("revision") not in paths
        or not isinstance(locations, dict)
        or set(locations) != {"revision/write.py"}
        or not isinstance(location, dict)
        or set(location) != {"repository", "commit", "path", "sha256", "verification"}
        or not isinstance(files, dict)
        or location.get("path") != paths[message["revision"]]
        or location.get("verification") != "declared"
        or not isinstance(location.get("repository"), str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
            location["repository"],
        )
        or not isinstance(location.get("commit"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", location["commit"])
        or not isinstance(location.get("sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", location["sha256"])
        or location["sha256"] != files.get("revision/write.py")
    ):
        raise RuntimeError("invalid declared source_locations binding")


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
            "original application image must use repository@sha256:digest; prepare an immutable v1 baseline before plan"
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
    """Preserve configurable fields; absent optional capacity strategy means no entries."""
    value = {key: service.get(key) for key in SERVICE_SETTINGS}
    if value["capacityProviderStrategy"] is None:
        value["capacityProviderStrategy"] = []
    return value


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
        if (
            options["scenario"] != "write-column-regression"
            or options["revision"] != "v2"
        ):
            raise RuntimeError("only the new v2 write-column regression is supported")
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", options["image"] or ""):
            raise RuntimeError("fault image must be digest-pinned")
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
        if "VitalIngestFailures" not in {a.get("MetricName") for a in alarms}:
            raise RuntimeError("the ingest failure alarm is required")
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
        if options["image"] and options["image"].split("@")[-1] in self.image_digests(
            running, options["container"]
        ):
            raise RuntimeError("fault image equals the running healthy image")
        observations = self.normal_observations(
            original["taskDefinition"],
            running,
            options["container"],
            baseline,
            source_manifests,
        )
        snapshot = {
            "contractVersion": JOURNAL_VERSION,
            "normalObservations": observations,
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
        """Retain verified v1 startup manifests from each exact running task's log stream."""
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
                endTime=int(
                    datetime.fromisoformat(
                        task.get("startedAt", task["createdAt"])
                    ).timestamp()
                    * 1000
                )
                + 10 * 60 * 1000,
                filterPattern='{ $.event = "source_manifest" }',
            ).get("events", [])
            if not events:
                raise RuntimeError(
                    "verified v1 source_manifest is missing for a running baseline task"
                )
            for event in events:
                manifest = json.loads(event["message"])
                files = manifest.get("files")
                if (
                    event.get("logStreamName") != stream
                    or event.get("timestamp", 0) < created
                    or manifest.get("event") != "source_manifest"
                    or manifest.get("revision") != "v1"
                    or manifest.get("verified") is not True
                    or not isinstance(files, dict)
                    or not files
                    or "revision/write.py" not in files
                    or manifest.get("fingerprint")
                    != hashlib.sha256(
                        json.dumps(files, sort_keys=True).encode()
                    ).hexdigest()
                ):
                    raise RuntimeError(
                        "baseline source_manifest must verify v1 and its source fingerprint for the running task"
                    )
                validate_declared_source_locations(manifest)
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
                "baseline tasks report inconsistent v1 source fingerprints"
            )
        return evidence

    def normal_observations(self, definition, tasks, name, metrics, manifests):
        """Bind safe logger dictionaries to each running task and fresh normal window.

        Startup source/SQL contracts may precede the window; schema and committed
        writes must occur inside it. Missing diagnostics cannot become a baseline.
        """
        group = container(definition, name)["logConfiguration"]["options"][
            "awslogs-group"
        ]
        start = int(datetime.fromisoformat(metrics["start"]).timestamp() * 1000)
        end = int(datetime.fromisoformat(metrics["end"]).timestamp() * 1000)
        allowed = {
            "event",
            "timestamp",
            "level",
            "name",
            "service",
            "message",
            "observed_at",
            "revision",
            "verified",
            "files",
            "source_locations",
            "fingerprint",
            "base_fingerprint",
            "built_at",
            "request_id",
            "operation",
            "sql_hash",
            "sql_hash_algorithm",
            "schema_name",
            "table_name",
            "column_names",
            "count",
            "completion_semantics",
            "metric_namespace",
            "service_name",
            "attempt_metric",
            "failure_metric",
            "attempt_semantics",
            "failure_semantics",
            "cancellation_semantics",
            "success_evidence_event",
            "success_count_field",
            "success_semantics",
            "input_contract",
            "input_contract_sha256",
        }
        result = []
        for task, manifest in zip(tasks, manifests, strict=True):
            stream = manifest["logStreamName"]
            created = int(datetime.fromisoformat(task["createdAt"]).timestamp() * 1000)
            events = list(manifest["events"])
            for lower, upper, pattern in (
                (
                    created,
                    min(end, created + 600000),
                    '{ $.event = "write_contract" || $.event = "write_accounting" }',
                ),
                (
                    start,
                    end,
                    '{ $.event = "write_completed" || $.event = "db_schema_snapshot" || $.event = "input_contract_observed" }',
                ),
            ):
                response = self.aws(
                    "logs",
                    "filter-log-events",
                    logGroupName=group,
                    logStreamNames=[stream],
                    startTime=lower,
                    endTime=upper,
                    filterPattern=pattern,
                )
                if response.get("nextToken"):
                    raise RuntimeError(
                        "normal log query incomplete; narrow the capture before planning"
                    )
                events.extend(response.get("events", []))
            chosen = {}
            input_events = []
            for event in events:
                message = json.loads(event["message"])
                kind = message.get("event")
                stamp = event.get("timestamp", 0)
                if kind not in {
                    "source_manifest",
                    "write_completed",
                    "db_schema_snapshot",
                    "write_contract",
                    "write_accounting",
                    "input_contract_observed",
                }:
                    raise RuntimeError("unexpected normal diagnostic event")
                if (
                    kind
                    in {
                        "write_completed",
                        "db_schema_snapshot",
                        "input_contract_observed",
                    }
                    and stamp < start
                ):
                    continue
                if (
                    event.get("logStreamName") != stream
                    or not event.get("eventId")
                    or not created <= stamp < end
                    or set(message) - allowed
                    or not message.get("observed_at")
                    or abs(
                        datetime.fromisoformat(message["observed_at"]).timestamp()
                        * 1000
                        - stamp
                    )
                    > 60000
                ):
                    raise RuntimeError(
                        "normal diagnostic source/time/field contract mismatch"
                    )
                if "message" in message and message["message"] != kind:
                    raise RuntimeError("normal diagnostic contains unstructured prose")
                if (
                    "service" in message
                    and message["service"] != "healthcare-sensor-app"
                ):
                    raise RuntimeError("normal diagnostic logger service mismatch")
                validate_declared_source_locations(message)
                if kind == "write_accounting":
                    expected = {
                        "metric_namespace": "Healthcare/Sensor",
                        "service_name": "healthcare-sensor-app",
                        "attempt_metric": "VitalIngestAttempts",
                        "failure_metric": "VitalIngestFailures",
                        "attempt_semantics": "completed_successful_rows_plus_failed_rows",
                        "failure_semantics": "failed_rows",
                        "cancellation_semantics": "excluded_from_completed_counters",
                        "success_evidence_event": "write_completed",
                        "success_count_field": "count",
                        "success_semantics": "committed_rows",
                    }
                    if any(
                        message.get(key) != value for key, value in expected.items()
                    ):
                        raise RuntimeError(
                            "normal completed-write accounting is not proven"
                        )
                if kind in {"write_completed", "write_contract", "db_schema_snapshot"}:
                    columns = message.get("column_names", [])
                    if (
                        message.get("table_name") != "sensor_readings"
                        or "timestamp" not in columns
                        or "sampled_at" in columns
                        or not isinstance(columns, list)
                        or any(
                            not re.fullmatch(r"[a-z_][a-z0-9_]*", c) for c in columns
                        )
                    ):
                        raise RuntimeError(
                            "normal schema/write column precondition not proven"
                        )
                if kind == "db_schema_snapshot" and not re.fullmatch(
                    r"[a-z_][a-z0-9_]*", message.get("schema_name") or ""
                ):
                    raise RuntimeError("actual schema identity missing")
                if kind in {"write_completed", "write_contract"} and not re.fullmatch(
                    r"[a-f0-9]{64}", message.get("sql_hash") or ""
                ):
                    raise RuntimeError("actual INSERT fingerprint missing")
                if kind == "write_completed" and (
                    type(message.get("count")) is not int
                    or message["count"] <= 0
                    or message.get("completion_semantics") != "committed_rows"
                ):
                    raise RuntimeError("committed normal write not proven")
                captured = {
                    "message": message,
                    "timestamp": stamp,
                    "event_id": event["eventId"],
                    "log_group": group,
                    "log_stream": stream,
                }
                if kind == "input_contract_observed":
                    input_events.append(captured)
                else:
                    chosen[kind] = captured
            if set(chosen) != {
                "source_manifest",
                "write_completed",
                "db_schema_snapshot",
                "write_contract",
                "write_accounting",
            }:
                raise RuntimeError(
                    "fresh schema, source, INSERT contract and committed write are required"
                )
            if (
                chosen["write_completed"]["message"]["sql_hash"]
                != chosen["write_contract"]["message"]["sql_hash"]
            ):
                raise RuntimeError("normal INSERT fingerprints disagree")
            result.extend(chosen.values())
            # Optional: preserve only the actual input receipt for the selected
            # committed request. Absence keeps old baselines valid, not early READY.
            write = chosen["write_completed"]
            result.extend(
                event
                for event in input_events
                if event["message"].get("request_id")
                and event["message"]["request_id"] == write["message"].get("request_id")
                and event["timestamp"] <= write["timestamp"]
            )
        if len(result) > 100:
            raise RuntimeError("normal observation count exceeds reader budget")
        return result

    def publish_baseline(self, metrics):
        """Publish canonical actual evidence create-only, then verify stored bytes.

        A timed-out PUT may have succeeded. GET and a full content hash settle
        that uncertainty; ETag and a partial PUT response are not content proof.
        """
        snapshot = self.journal.snapshot
        options = snapshot["options"]
        service = self.service()
        self.assert_owner(service)
        tasks = self.tasks(serviceName=self.target["service"])
        if not stable(
            service, snapshot["service"]["taskDefinition"]
        ) or not self.tasks_match(
            tasks,
            service["taskDefinition"],
            service["desiredCount"],
            options["container"],
        ):
            raise RuntimeError("normal deployment changed before publication")
        definition = snapshot["taskDefinition"]["taskDefinition"]
        immutable_baseline_image(definition, tasks, options["container"])
        manifests = self.baseline_source_manifests(
            definition, tasks, options["container"]
        )
        observations = self.normal_observations(
            definition, tasks, options["container"], metrics, manifests
        )
        payload = {
            "schema_version": 1,
            "run_id": options["run_id"],
            "observed_at": now(),
            "scope": {
                "account_id": self.target["account"],
                "region": self.target["region"],
                "cluster_arn": service["clusterArn"],
                "service_arn": service["serviceArn"],
                "service_name": service.get(
                    "serviceName", service["serviceArn"].rsplit("/", 1)[-1]
                ),
                "container_name": options["container"],
                "log_group": observations[0]["log_group"],
                "desired_count": service["desiredCount"],
            },
            "normal": {
                "task_definition_arn": service["taskDefinition"],
                "image_digest": snapshot["image"].split("@")[1],
            },
            "service_settings": settings(service),
            "metrics": {
                key: {
                    "namespace": "Healthcare/Sensor",
                    "metric_name": metric,
                    "dimensions": {"ServiceName": "healthcare-sensor-app"},
                }
                for key, metric in (
                    ("attempts", "VitalIngestAttempts"),
                    ("failures", "VitalIngestFailures"),
                )
            },
            "metric_observations": {
                "start": metrics["start"],
                "end": metrics["end"],
                **{
                    key: [
                        {field: point[field] for field in ("Timestamp", "Sum", "Unit")}
                        for point in metrics["values"][metric]["Datapoints"]
                    ]
                    for key, metric in (
                        ("attempts", "VitalIngestAttempts"),
                        ("failures", "VitalIngestFailures"),
                    )
                },
            },
            "observations": observations,
        }
        raw = canonical(payload)
        if len(raw) > 512 * 1024:
            raise RuntimeError("normal baseline exceeds the agent reader size budget")
        reference = {
            "bucket": options["evidence_bucket"],
            "key": f"baselines/{options['run_id']}/normal.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        self.record(
            "baseline_put_intent", {"baseline_ref": reference, "baseline": payload}
        )
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "normal.json"
            body.write_bytes(raw)
            try:
                self.aws(
                    "s3api",
                    "put-object",
                    Bucket=reference["bucket"],
                    Key=reference["key"],
                    Body=str(body),
                    IfNoneMatch="*",
                    ContentType="application/json",
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                KeyError,
                TypeError,
                subprocess.SubprocessError,
            ) as error:
                self.record("baseline_put_uncertain", {"error": str(error)})
            downloaded = Path(directory) / "download.json"
            self.aws(
                "s3api",
                "get-object",
                Bucket=reference["bucket"],
                Key=reference["key"],
                OutputFile=str(downloaded),
            )
            if (
                hashlib.sha256(downloaded.read_bytes()).hexdigest()
                != reference["sha256"]
            ):
                raise RuntimeError("stored baseline digest mismatch")
        self.record("baseline_published", reference)
        return reference

    @staticmethod
    def alarm_configuration(alarm):
        """Use only PutMetricAlarm fields so existing criteria and actions survive."""
        return {key: alarm[key] for key in ALARM_SETTINGS if key in alarm}

    def decorate_alarms(self):
        """Publish only the incident pointer after the immutable baseline is verified."""
        snapshot = self.journal.snapshot
        reference = self.journal.find("baseline_published")[-1]["data"]
        for original in snapshot["alarms"]:
            if original.get("MetricName") != "VitalIngestFailures":
                continue
            current = self.alarms([original["AlarmName"]])[0]
            if self.alarm_configuration(current) != self.alarm_configuration(
                original
            ) or current.get("AlarmDescription", "") != original.get(
                "AlarmDescription", ""
            ):
                raise RuntimeError("alarm changed before decoration")
            metadata = {
                "summary": "Healthcare service symptoms",
                "run_id": snapshot["options"]["run_id"],
                "service": snapshot["service"]["serviceArn"],
                "baseline_ref": reference,
            }
            description = canonical(metadata).decode("utf-8")
            if len(description) > 1024:
                raise RuntimeError("alarm description exceeds 1024 characters")
            self.record(
                "alarm_decoration_intent",
                {"name": original["AlarmName"], "description": description},
            )
            self.aws(
                "cloudwatch",
                "put-metric-alarm",
                **self.alarm_configuration(original),
                AlarmDescription=description,
            )
            current = self.alarms([original["AlarmName"]])[0]
            if current.get(
                "AlarmDescription"
            ) != description or self.alarm_configuration(
                current
            ) != self.alarm_configuration(original):
                raise RuntimeError("alarm decoration not verified")
            self.record("alarm_decorated", {"name": original["AlarmName"]})

    def restore_alarm(self, original):
        """Restore original metadata even after a lost PUT, refusing foreign edits."""
        intents = [
            e
            for e in self.journal.find("alarm_decoration_intent")
            if e["data"]["name"] == original["AlarmName"]
        ]
        if not intents:
            return
        current = self.alarms([original["AlarmName"]])[0]
        if self.alarm_configuration(current) != self.alarm_configuration(original):
            raise RuntimeError("foreign alarm criteria; refusing overwrite")
        description = current.get("AlarmDescription", "")
        if description == original.get("AlarmDescription", ""):
            return
        if description != intents[-1]["data"]["description"]:
            raise RuntimeError("foreign alarm metadata; refusing overwrite")
        self.record(
            "restore_alarm_intent", {"name": original["AlarmName"]}, recovery=True
        )
        description_field = (
            {"AlarmDescription": original["AlarmDescription"]}
            if "AlarmDescription" in original
            else {}
        )
        self.aws(
            "cloudwatch",
            "put-metric-alarm",
            **self.alarm_configuration(original),
            **description_field,
        )
        self.record(
            "restore_alarm_response", {"name": original["AlarmName"]}, recovery=True
        )

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

    def register(self):
        """Clone the healthy definition with only immutable image and lineage changes."""
        snapshot = self.journal.snapshot
        options = snapshot["options"]
        td = copy.deepcopy(snapshot["taskDefinition"]["taskDefinition"])
        for key in READ_ONLY_TD:
            td.pop(key, None)
        app = container(td, options["container"])
        environment = env_map(td, options["container"])
        environment.update(
            RCA_TEST_RUN_ID=options["run_id"], DEPLOYED_REVISION=options["revision"]
        )
        app["image"] = options["image"]
        app["environment"] = [
            {"name": k, "value": v} for k, v in sorted(environment.items())
        ]
        self.assert_scenario_environment(td)
        tags = {
            t["key"]: t["value"]
            for t in snapshot["taskDefinition"].get("tags", [])
            if not t["key"].startswith("aws:")
        }
        td["tags"] = [
            {"key": k, "value": v} for k, v in (tags | self.owner_tags()).items()
        ]
        self.record("register_intent", {"kind": "service_revision", "definition": td})
        registered = self.aws("ecs", "register-task-definition", **td)
        arn = registered["taskDefinition"]["taskDefinitionArn"]
        self.record("service_revision", {"arn": arn, "response": registered})
        return arn

    def assert_scenario_environment(self, candidate):
        """Reject any service environment change outside the selected cause/lineage."""
        snapshot = self.journal.snapshot
        options = snapshot["options"]
        original = env_map(
            snapshot["taskDefinition"]["taskDefinition"], options["container"]
        )
        proposed = env_map(candidate, options["container"])
        permitted = {"RCA_TEST_RUN_ID", "DEPLOYED_REVISION"}
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
        if snapshot.get("contractVersion") != JOURNAL_VERSION:
            raise RuntimeError("legacy journal is read-only; use a fresh run id")
        immutable_baseline_image(
            snapshot["taskDefinition"]["taskDefinition"],
            snapshot["tasks"],
            snapshot["options"]["container"],
        )
        if not snapshot.get("sourceManifests"):
            raise RuntimeError(
                "snapshot lacks verified v1 baseline source evidence; create a new plan"
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
            self.publish_baseline(baseline)
            self.decorate_alarms()
            arn = self.register()
            self.assert_owner(self.service())
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
            rollout_ready = (
                stable(result["service"], expected)
                and self.tasks_match(
                    result["tasks"],
                    expected,
                    self.journal.snapshot["service"]["desiredCount"],
                    self.journal.snapshot["options"]["container"],
                )
                and self.image_digests(
                    result["tasks"], self.journal.snapshot["options"]["container"]
                )
                == [self.journal.snapshot["options"]["image"].split("@")[1]]
            )
            if not seconds or (rollout_ready and bool(revisions)):
                result["applyRolloutObserved"] = rollout_ready
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError("scenario rollout observation timed out")
            time.sleep(min(10, max(0, deadline - time.monotonic())))

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
        """Two complete positive attempt bins and explicit zero failures prove activity."""
        if not metrics.get("ready"):
            return False
        start = datetime.fromisoformat(metrics["start"])
        expected = {start, start + timedelta(minutes=1)}
        for metric, positive in (
            ("VitalIngestAttempts", True),
            ("VitalIngestFailures", False),
        ):
            points = metrics["values"].get(metric, {}).get("Datapoints", [])
            if (
                len(points) != 2
                or {datetime.fromisoformat(p["Timestamp"]) for p in points} != expected
            ):
                return False
            if any(
                p.get("Unit") != "Count"
                or not isinstance(p.get("Sum"), (int, float))
                or isinstance(p.get("Sum"), bool)
                or not math.isfinite(p["Sum"])
                or (p["Sum"] <= 0 if positive else p["Sum"] != 0)
                for p in points
            ):
                return False
        return True

    def status(self, *, recovery=False):
        """Observe recovery conservatively, returning logging failures during cleanup."""
        snapshot = self.journal.snapshot
        if snapshot.get("contractVersion") != JOURNAL_VERSION:
            raise RuntimeError("legacy journal is read-only; retain it unchanged")
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
        alarms = self.alarms(snapshot["options"]["alarms"])
        restore = self.journal.find("restore_intent")
        original = snapshot["service"]["taskDefinition"]
        original_tasks = self.tasks_match(
            tasks, original, snapshot["service"]["desiredCount"]
        )
        digest_match = self.image_digests(
            tasks, snapshot["options"]["container"]
        ) == self.image_digests(snapshot["tasks"], snapshot["options"]["container"])
        configuration_restored = (
            ownership_error is None
            and stable(service, original)
            and original_tasks
            and digest_match
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
            "alarmsOk": all(a["StateValue"] == "OK" for a in alarms),
            "alarmSettingsUnchanged": original_alarms == live_alarms,
            "successfulFreshSymptoms": self.metrics_healthy(metrics, alarms),
            "alarmMetadataRestored": all(
                a.get("AlarmDescription", "")
                == next(
                    o.get("AlarmDescription", "")
                    for o in snapshot["alarms"]
                    if o["AlarmName"] == a["AlarmName"]
                )
                for a in alarms
            ),
        }
        result = {
            "runId": snapshot["options"]["run_id"],
            "service": service,
            "tasks": tasks,
            "alarms": alarms,
            "metrics": metrics,
            "checks": checks,
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

    def restore(self, wait_seconds=0):
        """Persist cleanup evidence before release; keep journal failures unresolved."""
        if self.journal.snapshot.get("contractVersion") != JOURNAL_VERSION:
            raise RuntimeError(
                "legacy journal is read-only; use its historical cleanup tooling"
            )
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
        # Metadata cleanup remains independent of service rollback failures.
        for original_alarm in self.journal.snapshot["alarms"]:
            try:
                self.restore_alarm(original_alarm)
            except RECOVERABLE_ERRORS as error:
                errors.append(
                    {
                        "step": "alarm:" + original_alarm["AlarmName"],
                        "error": str(error),
                    }
                )
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
        choices=["write-column-regression"],
        default="write-column-regression",
    )
    parser.add_argument("--container", default="healthcare")
    parser.add_argument("--image", help="Fault repository@sha256:digest")
    parser.add_argument("--revision", choices=["v2"], default="v2")
    parser.add_argument("--evidence-bucket", help="Configured agent evidence bucket")
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
        help="Required ingest failure alarm; optional additional alarms",
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
        "--evidence-bucket",
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
    if args.wait_seconds < 0:
        parser.error("wait-seconds must be nonnegative")
    args.expect_settings = {}
    for entry in args.expect_setting:
        key, separator, value = entry.partition("=")
        if not separator or key not in SETTINGS:
            parser.error("--expect-setting requires a controlled KEY=VALUE")
        args.expect_settings[key] = value
    if args.action == "plan":
        if not args.alarm or not args.evidence_bucket:
            parser.error("plan requires --alarm and --evidence-bucket")
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", args.image or ""):
            parser.error("plan requires immutable --image repository@sha256:digest")
    return args


def interrupted(signum, frame):
    """Convert termination into normal failure cleanup when Python can handle it."""
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv=None):
    """Run one command under a nonblocking service lock and print JSON evidence."""
    args = parse_args(argv)
    aws = Aws(args.region, args.profile)
    print(
        "ECS_MODEL_PREFLIGHT " + json.dumps(aws.preflight(), sort_keys=True),
        file=sys.stderr,
    )
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
                    "evidence_bucket",
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
