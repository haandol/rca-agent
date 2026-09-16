"""Deterministic cloud model for the realistic demo orchestration contract."""

import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_realistic_demo.py"
spec = importlib.util.spec_from_file_location("realistic_demo", SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class Clock(datetime):
    """Hold time fixed so minute-window recovery checks are deterministic."""

    current = datetime(2026, 9, 10, 12, 0, 5, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        """Return the deterministic test timestamp."""
        return cls.current


class PollClock:
    """Advance wall/monotonic time deterministically without real polling delays."""

    def __init__(self, on_sleep=None):
        """Start a fresh wait budget and optional asynchronous cloud transition."""
        self.elapsed = 0
        self.on_sleep = on_sleep

    def monotonic(self):
        """Return elapsed simulated seconds."""
        return self.elapsed

    def sleep(self, seconds):
        """Advance both clocks before executing an optional state transition."""
        self.elapsed += seconds
        Clock.current += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep(self.elapsed)


class Cloud:
    """Stateful AWS double with real task-definition, tag and rollout boundaries."""

    def __init__(self):
        """Create a healthy private service and three correctly bound alarms."""
        self.calls = []
        self.maintenance_task_arn = "arn:task/owned"
        self.target = {
            "account": "123456789012",
            "region": "us-east-1",
            "cluster": "arn:cluster",
            "service": "arn:service",
        }
        self.original = "arn:task-definition/healthcare:1"
        self.healthy_digest = "sha256:" + "a" * 64
        self.service = {
            "serviceArn": "arn:service",
            "clusterArn": "arn:cluster",
            "status": "ACTIVE",
            "taskDefinition": self.original,
            "desiredCount": 1,
            "runningCount": 1,
            "pendingCount": 0,
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "subnets": ["subnet-1"],
                    "securityGroups": ["sg-1"],
                    "assignPublicIp": "DISABLED",
                }
            },
            "deploymentConfiguration": {
                "minimumHealthyPercent": 100,
                "maximumPercent": 200,
            },
            "deploymentController": {"type": "ECS"},
            "launchType": "FARGATE",
            "platformVersion": "1.4.0",
            "schedulingStrategy": "REPLICA",
            "enableExecuteCommand": True,
            "tags": [{"key": "Project", "value": "keep"}],
        }
        self.definitions = {
            self.original: {
                "taskDefinition": {
                    "taskDefinitionArn": self.original,
                    "family": "healthcare",
                    "revision": 1,
                    "networkMode": "awsvpc",
                    "requiresCompatibilities": ["FARGATE"],
                    "taskRoleArn": "arn:role/task",
                    "executionRoleArn": "arn:role/execution",
                    "cpu": "512",
                    "memory": "1024",
                    "containerDefinitions": [
                        {
                            "name": "healthcare",
                            "image": "registry.example/healthcare@"
                            + self.healthy_digest,
                            "logConfiguration": {
                                "logDriver": "awslogs",
                                "options": {
                                    "awslogs-group": "/ecs/healthcare",
                                    "awslogs-stream-prefix": "healthcare",
                                    "awslogs-region": "us-east-1",
                                },
                            },
                            "environment": [
                                {"name": "DB_POOL_SIZE", "value": "5"},
                                {"name": "DB_MAX_OVERFLOW", "value": "10"},
                                {"name": "TRAFFIC_SEED", "value": "123"},
                            ],
                            "secrets": [
                                {"name": "DB_PASSWORD", "valueFrom": "arn:secret"}
                            ],
                            "healthCheck": {"command": ["CMD-SHELL", "healthz"]},
                            "dependsOn": [
                                {"containerName": "otel", "condition": "START"}
                            ],
                        },
                        {
                            "name": "otel",
                            "image": "collector:latest",
                            "essential": False,
                        },
                    ],
                },
                "tags": [{"key": "Project", "value": "keep"}],
            }
        }
        self.objects = {}
        self.put_failure = False
        self.corrupt_object = False
        self.omit_event = None
        self.bad_schema = False
        self.unsafe_event = False
        self.jobs = {}
        self.alarms = [
            {
                "AlarmName": name,
                "MetricName": metric,
                "Namespace": namespace,
                "Dimensions": dimensions,
                "StateValue": "OK",
                "Threshold": threshold,
                "Period": 60,
                "EvaluationPeriods": 2,
                "Unit": "Milliseconds"
                if metric == "PatientVitalsQueryDuration"
                else "Count",
                "ComparisonOperator": "GreaterThanOrEqualToThreshold",
                "Statistic": "Average"
                if metric == "PatientVitalsQueryDuration"
                else "Sum",
            }
            for name, metric, namespace, dimensions, threshold in [
                (
                    "ingest",
                    "VitalIngestFailures",
                    "Healthcare/Sensor",
                    [{"Name": "ServiceName", "Value": "healthcare-sensor-app"}],
                    1,
                ),
                (
                    "query",
                    "PatientVitalsQueryDuration",
                    "Healthcare/Sensor",
                    [{"Name": "ServiceName", "Value": "healthcare-sensor-app"}],
                    500,
                ),
                (
                    "connections",
                    "DatabaseConnections",
                    "AWS/RDS",
                    [{"Name": "DBInstanceIdentifier", "Value": "db"}],
                    12,
                ),
            ]
        ]
        self.missing_metrics = False
        self.fail_update = False
        self.fail_stop = False
        self.lost_run_response = False
        self.failed_rollout = False
        self.async_restore = False
        self.async_fault = False
        files = {"revision/write.py": "1" * 64, "revision/session.py": "2" * 64}
        self.source_manifest = {
            "event": "source_manifest",
            "revision": "v1",
            "verified": True,
            "files": files,
            "fingerprint": hashlib.sha256(
                json.dumps(files, sort_keys=True).encode()
            ).hexdigest(),
        }
        self.source_events_missing = False
        self.rollout(self.original)

    def rollout(self, arn):
        """Model a completed rollout, keeping image digest evidence explicit."""
        self.service["taskDefinition"] = arn
        self.service["runningCount"] = self.service["desiredCount"]
        self.service["pendingCount"] = 0
        self.service["deployments"] = [
            {"status": "PRIMARY", "taskDefinition": arn, "rolloutState": "COMPLETED"}
        ]
        image = self.definitions[arn]["taskDefinition"]["containerDefinitions"][0][
            "image"
        ]
        self.running = [
            {
                "taskArn": "arn:task/service",
                "createdAt": (Clock.current - timedelta(minutes=10)).isoformat(),
                "taskDefinitionArn": arn,
                "lastStatus": "RUNNING",
                "containers": [
                    {
                        "name": "healthcare",
                        "image": image,
                        "imageDigest": image.split("@")[1]
                        if "@" in image
                        else self.healthy_digest,
                    }
                ],
            }
        ]

    def __call__(self, service, operation, /, **payload):
        """Execute only explicitly modeled calls and return isolated AWS-like JSON."""
        self.calls.append((service, operation, copy.deepcopy(payload)))
        if operation == "get-caller-identity":
            result = {"Account": self.target["account"]}
        elif operation == "describe-services":
            result = {"services": [self.service]}
        elif operation == "describe-task-definition":
            result = self.definitions[payload["taskDefinition"]]
        elif operation == "list-tasks":
            if "serviceName" in payload:
                result = {"taskArns": [task["taskArn"] for task in self.running]}
            else:
                result = {
                    "taskArns": [
                        arn
                        for arn, task in self.jobs.items()
                        if task["startedBy"] == payload["startedBy"]
                        and task["lastStatus"] != "STOPPED"
                    ]
                }
        elif operation == "describe-tasks":
            all_tasks = self.jobs | {t["taskArn"]: t for t in self.running}
            result = {"tasks": [all_tasks[arn] for arn in payload["tasks"]]}
        elif operation == "describe-alarms":
            result = {
                "MetricAlarms": [
                    a for a in self.alarms if a["AlarmName"] in payload["AlarmNames"]
                ]
            }
        elif operation == "filter-log-events":
            stamp = payload["startTime"] + 1000
            observed = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
            contract = {
                "observed_at": observed,
                "table_name": "sensor_readings",
                "schema_name": "public",
                "column_names": ["timestamp"],
                "sql_hash": "3" * 64,
            }
            candidates = [
                dict(self.source_manifest, observed_at=observed),
                dict(contract, event="write_contract", schema_name=None),
                dict(contract, event="db_schema_snapshot"),
                dict(
                    contract,
                    event="write_completed",
                    schema_name=None,
                    count=1,
                    completion_semantics="committed_rows",
                ),
                {
                    "event": "write_accounting",
                    "observed_at": observed,
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
                },
            ]
            for candidate in candidates:
                candidate["service"] = "healthcare-sensor-app"
            if self.bad_schema:
                candidates[2]["column_names"] = ["sampled_at"]
            if self.unsafe_event:
                candidates[3]["parameters"] = "PRIVATE_SENTINEL"
            result = {
                "events": []
                if self.source_events_missing
                else [
                    {
                        "eventId": item["event"],
                        "logStreamName": payload["logStreamNames"][0],
                        "timestamp": stamp,
                        "message": json.dumps(item),
                    }
                    for item in candidates
                    if item["event"] in payload["filterPattern"]
                    and item["event"] != self.omit_event
                ]
            }
        elif operation == "put-object":
            key = (payload["Bucket"], payload["Key"])
            assert payload["IfNoneMatch"] == "*"
            if key in self.objects:
                raise RuntimeError("PreconditionFailed")
            self.objects[key] = Path(payload["Body"]).read_bytes()
            if self.corrupt_object:
                self.objects[key] += b" "
            if self.put_failure:
                raise RuntimeError("lost PUT response")
            result = {}  # Deliberately partial; only GET bytes can prove the write.
        elif operation == "get-object":
            Path(payload["OutputFile"]).write_bytes(
                self.objects[(payload["Bucket"], payload["Key"])]
            )
            result = {}
        elif operation == "put-metric-alarm":
            alarm = next(
                a for a in self.alarms if a["AlarmName"] == payload["AlarmName"]
            )
            alarm.pop("AlarmDescription", None)
            alarm.update(payload)
            result = {}
        elif operation == "get-metric-statistics":
            start = datetime.fromisoformat(payload["StartTime"])
            metric = payload["MetricName"]
            value = 0 if metric == "VitalIngestFailures" else 20
            result = {
                "Datapoints": []
                if self.missing_metrics
                else [
                    {
                        "Timestamp": (start + timedelta(minutes=i)).isoformat(),
                        "Sum": value,
                        "Average": value,
                        "SampleCount": 1,
                        "Unit": payload["Unit"],
                    }
                    for i in range(2)
                ]
            }
        elif operation == "tag-resource":
            self.service["tags"].extend(payload["tags"])
            result = {}
        elif operation == "untag-resource":
            self.service["tags"] = [
                tag
                for tag in self.service["tags"]
                if tag["key"] not in payload["tagKeys"]
            ]
            result = {}
        elif operation == "register-task-definition":
            arn = f"arn:task-definition/healthcare:{len(self.definitions) + 1}"
            td = copy.deepcopy(payload)
            tags = td.pop("tags")
            td["taskDefinitionArn"] = arn
            self.definitions[arn] = {"taskDefinition": td, "tags": tags}
            result = self.definitions[arn]
        elif operation == "update-service":
            previous_tasks = copy.deepcopy(self.running)
            previous_arn = self.service["taskDefinition"]
            self.rollout(payload["taskDefinition"])
            if self.async_fault and payload["taskDefinition"] != self.original:
                self.running = previous_tasks
                self.service["pendingCount"] = 1
                self.service["deployments"][0]["rolloutState"] = "IN_PROGRESS"
            if self.async_restore and payload["taskDefinition"] == self.original:
                self.running = previous_tasks
                self.service["pendingCount"] = 1
                self.service["deployments"][0]["rolloutState"] = "IN_PROGRESS"
                self.service["deployments"].append(
                    {
                        "status": "ACTIVE",
                        "taskDefinition": previous_arn,
                        "rolloutState": "COMPLETED",
                    }
                )
            if self.failed_rollout:
                self.failed_rollout = False
                self.service["deployments"][0]["rolloutState"] = "FAILED"
            if self.fail_update:
                self.fail_update = False
                raise RuntimeError("update response lost after AWS mutation")
            result = {"service": self.service}
        elif operation == "run-task":
            task = {
                "taskArn": self.maintenance_task_arn,
                "taskDefinitionArn": payload["taskDefinition"],
                "startedBy": payload["startedBy"],
                "lastStatus": "RUNNING",
                "tags": payload["tags"],
            }
            self.jobs[task["taskArn"]] = task
            if self.lost_run_response:
                raise RuntimeError("RunTask response lost")
            result = {"tasks": [task]}
        elif operation == "stop-task":
            if self.fail_stop:
                raise RuntimeError("stop failed")
            self.jobs[payload["task"]]["lastStatus"] = "STOPPED"
            self.jobs[payload["task"]]["tags"] = []
            self.jobs[payload["task"]]["stopCode"] = "UserInitiated"
            result = {"task": self.jobs[payload["task"]]}
        else:
            raise AssertionError(f"Unexpected API: {service} {operation}")
        return copy.deepcopy(result)


class DemoTests(unittest.TestCase):
    """Verify lifecycle and failure contracts without contacting AWS."""

    def setUp(self):
        """Isolate the filesystem and cloud per case."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        Clock.current = datetime(2026, 9, 10, 12, 0, 5, tzinfo=timezone.utc)
        self.cloud = Cloud()
        self.journal = demo.Journal(Path(self.temp.name) / "run-1")
        self.runner = demo.Demo(self.cloud, self.journal, self.cloud.target)
        Clock.current = datetime(2026, 9, 10, 12, 0, 5, tzinfo=timezone.utc)
        self.old_datetime = demo.datetime
        demo.datetime = Clock
        self.addCleanup(setattr, demo, "datetime", self.old_datetime)

    def plan(self, scenario="write-column-regression", **changes):
        """Freeze caller-supplied controls with deterministic healthy evidence."""
        options = {
            "run_id": "run-1",
            "scenario": scenario,
            "container": "healthcare",
            "image": "registry.example/healthcare@sha256:" + "b" * 64,
            "revision": "v2",
            "evidence_bucket": "configured-evidence",
            "pool_size": 1,
            "max_overflow": 0,
            "pool_timeout": 0.25,
            "hold_seconds": 180,
            "expect_settings": {"TRAFFIC_SEED": "123"},
            "alarms": ["ingest", "query", "connections"],
        }
        options.update(changes)
        return self.runner.plan(options)

    def advance(self):
        """Advance beyond two full fresh minute buckets."""
        Clock.current += timedelta(minutes=4)

    def polling_clock(self, on_sleep=None):
        """Install an isolated simulated polling clock for this test."""
        previous = demo.time
        clock = PollClock(on_sleep)
        demo.time = clock
        self.addCleanup(setattr, demo, "time", previous)
        return clock

    def complete_restore_later(self, elapsed):
        """Finish ECS rollout first, then expose recovered alarms later."""
        if elapsed >= 30:
            self.cloud.rollout(self.cloud.original)
        if elapsed >= 180:
            for alarm in self.cloud.alarms:
                alarm["StateValue"] = "OK"

    def connect_cli(self):
        """Share the test journal with the canonical CLI target without using AWS."""
        previous = demo.Aws
        demo.Aws = lambda *args: self.cloud
        self.addCleanup(setattr, demo, "Aws", previous)
        path = Path(self.temp.name) / demo.digest(self.cloud.target) / "run-1"
        self.journal = demo.Journal(path)
        self.runner = demo.Demo(self.cloud, self.journal, self.cloud.target)
        return [
            "--run-id",
            "run-1",
            "--cluster",
            "cluster-name",
            "--service",
            "service-name",
            "--region",
            "us-east-1",
            "--journal-root",
            self.temp.name,
        ]

    @contextlib.contextmanager
    def failing_journal(self, *kinds, persistent=False, after=None):
        """Fail selected durable writes, optionally all later writes, including CLI reloads."""
        original = demo.Journal.append
        attempts = []
        stderr = io.StringIO()
        failed = False

        def append(journal, kind, data):
            """Inject storage loss before publication without fabricating durable events."""
            nonlocal failed
            attempts.append(kind)
            if (kind in kinds and (after is None or journal.find(after))) or (
                persistent and failed
            ):
                failed = True
                raise OSError("synthetic journal storage failure")
            return original(journal, kind, data)

        with (
            patch.object(demo.Journal, "append", append),
            contextlib.redirect_stderr(stderr),
        ):
            yield attempts, stderr

    def add_owned_service_change(self):
        """Model a persisted owned update alongside maintenance to exercise both branches."""
        arn = "arn:task-definition/healthcare:99"
        self.cloud.definitions[arn] = copy.deepcopy(
            self.cloud.definitions[self.cloud.original]
        )
        self.cloud.definitions[arn]["taskDefinition"]["taskDefinitionArn"] = arn
        self.journal.append("service_revision", {"arn": arn})
        self.journal.append("update_intent", {"arn": arn})
        self.cloud.rollout(arn)

    def assert_journal_failure_retains_owner(self, result, stderr):
        """Require visible failure, pending recovery and retained exact ownership."""
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(result["restorationState"], "pending")
        self.assertTrue(result["journalErrors"])
        self.assertIn("journal append failed", stderr.getvalue())
        tags = {t["key"]: t["value"] for t in self.cloud.service["tags"]}
        for key, value in self.runner.owner_tags().items():
            self.assertEqual(tags[key], value)
        self.assertFalse(self.journal.find("released"))

    def test_lost_update_response_attempts_exact_rollback(self):
        """An update transport failure still rolls back the already changed service."""
        self.plan()
        self.cloud.fail_update = True
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.runner.apply()
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertTrue(self.journal.find("apply_error"))
        self.assertTrue(self.journal.find("restore_result"))

    def apply_full_arn_maintenance(self):
        """Use a complete task ARN to exercise exact receipt identity comparisons."""
        self.cloud.maintenance_task_arn = (
            "arn:aws:ecs:us-east-1:123456789012:task/demo-cluster/"
            "1234567890abcdef1234567890abcdef"
        )
        self.plan("maintenance-lock")
        self.runner.apply()
        return self.cloud.jobs[self.cloud.maintenance_task_arn]

    def replay_journal(self, transform):
        """Rebuild valid hash links while deliberately changing selected proof events."""
        journal = demo.Journal(Path(self.temp.name) / "replayed")
        for event in self.journal.events:
            event = transform(copy.deepcopy(event))
            if event is not None:
                journal.append(event["kind"], event["data"])
        self.assertEqual(journal.events[0]["hash"], self.journal.events[0]["hash"])
        return demo.Demo(self.cloud, journal, self.cloud.target)

    def test_dirty_baseline_or_settings_mismatch_refuses_before_mutation(self):
        """Plans cannot start from a foreign claim, active alarm or mismatched load."""
        self.cloud.service["tags"].append({"key": demo.OWNER, "value": "foreign"})
        with self.assertRaisesRegex(RuntimeError, "dirty"):
            self.plan()
        self.assertFalse(self.journal.events)
        self.cloud.service["tags"].pop()
        with self.assertRaisesRegex(RuntimeError, "baseline setting mismatch"):
            self.plan(expect_settings={"TRAFFIC_SEED": "999"})
        self.cloud.alarms[0]["StateValue"] = "ALARM"
        with self.assertRaisesRegex(RuntimeError, "not all OK"):
            self.plan()

    def test_missing_metrics_and_running_image_digest_drift_never_verify_recovery(self):
        """Alarm OK and task-definition restoration alone do not prove recovery."""
        self.plan()
        self.runner.apply()
        self.runner.restore()
        self.advance()
        self.cloud.missing_metrics = True
        self.assertFalse(self.runner.restore()["recoveryVerified"])
        self.cloud.missing_metrics = False
        self.cloud.running[0]["containers"][0]["imageDigest"] = "sha256:" + "c" * 64
        self.assertFalse(self.runner.restore()["recoveryVerified"])
        self.assertFalse(self.journal.find("released"))

    def test_mutable_baseline_tags_are_refused_before_any_cloud_mutation(self):
        """A source-looking tag or v1 environment label cannot make restore immutable."""
        original = self.cloud.definitions[self.cloud.original]["taskDefinition"]
        original["containerDefinitions"][0]["environment"].append(
            {"name": "DEPLOYED_REVISION", "value": "v1"}
        )
        for image in (
            "registry.example/healthcare:latest",
            "registry.example/healthcare:v1",
            "registry.example/healthcare:git-abc123",
        ):
            with (
                self.subTest(image=image),
                self.assertRaisesRegex(
                    RuntimeError, "original application image must use"
                ),
            ):
                original["containerDefinitions"][0]["image"] = image
                self.plan()
        self.assertFalse(self.journal.events)
        self.assertFalse(
            any(
                operation
                in {
                    "tag-resource",
                    "register-task-definition",
                    "update-service",
                    "run-task",
                }
                for _, operation, _ in self.cloud.calls
            )
        )

    def test_baseline_digest_reference_must_match_running_image(self):
        """A pinned reference is insufficient when observed running code disagrees."""
        original = self.cloud.definitions[self.cloud.original]["taskDefinition"]
        original["containerDefinitions"][0]["image"] = (
            "registry.example/healthcare@sha256:" + "b" * 64
        )
        with self.assertRaisesRegex(RuntimeError, "does not match every running"):
            self.plan()
        self.assertFalse(self.journal.events)

    def test_baseline_requires_verified_v1_source_manifest_from_its_task_stream(self):
        """Reject missing, unbuilt, fault-revision and corrupt runtime source evidence."""
        valid = copy.deepcopy(self.cloud.source_manifest)
        for change in (
            {"revision": "r2"},
            {"revision": "r3"},
            {"verified": False},
            {"fingerprint": "0" * 64},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(RuntimeError, "source_manifest must verify v1"),
            ):
                self.cloud.source_manifest = valid | change
                self.plan()
        self.cloud.source_manifest = valid
        self.cloud.source_events_missing = True
        with self.assertRaisesRegex(RuntimeError, "source_manifest is missing"):
            self.plan()
        self.assertFalse(self.journal.events)
        self.cloud.source_events_missing = False
        self.plan()
        evidence = self.journal.snapshot["sourceManifests"][0]
        self.assertEqual(evidence["taskArn"], self.cloud.running[0]["taskArn"])
        self.assertEqual(evidence["logStreamName"], "healthcare/healthcare/service")
        self.assertEqual(json.loads(evidence["events"][0]["message"])["revision"], "v1")
        self.assertFalse(
            any(
                operation
                in {
                    "tag-resource",
                    "register-task-definition",
                    "update-service",
                    "run-task",
                }
                for _, operation, _ in self.cloud.calls
            )
        )

    def test_original_digest_restores_even_if_repository_tags_move(self):
        """Repointed repository tags cannot change the code selected by exact restore."""
        self.plan()
        original_image = self.journal.snapshot["image"]
        original_digest = original_image.split("@")[1]
        self.runner.apply()
        # The double uses this fallback digest only when an image uses a mutable tag.
        self.cloud.healthy_digest = "sha256:" + "c" * 64
        self.runner.restore()
        self.advance()
        result = self.runner.restore()
        self.assertTrue(result["recoveryVerified"])
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertEqual(
            self.cloud.running[0]["containers"][0]["image"], original_image
        )
        self.assertEqual(
            self.cloud.running[0]["containers"][0]["imageDigest"], original_digest
        )

    def test_foreign_service_owner_refuses_rollback(self):
        """A stolen service claim cannot trigger an update or tag removal."""
        self.plan()
        self.runner.apply()
        self.cloud.service["tags"] = [{"key": demo.OWNER, "value": "foreign"}]
        count = len(self.cloud.calls)
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertFalse(
            any(
                op in {"update-service", "untag-resource"}
                for _, op, _ in self.cloud.calls[count:]
            )
        )

    def test_immutable_journal_detects_tampering_and_run_reuse(self):
        """Evidence cannot be overwritten and corrupted chains reject recovery."""
        self.plan()
        with self.assertRaisesRegex(RuntimeError, "already has immutable"):
            self.plan()
        self.runner.apply()
        with self.assertRaisesRegex(RuntimeError, "single-use"):
            self.runner.apply()
        path = self.journal.path / "000000.json"
        event = json.loads(path.read_text())
        event["data"]["image"] = "tampered"
        path.write_text(json.dumps(event))
        with self.assertRaisesRegex(RuntimeError, "integrity mismatch"):
            demo.Journal(self.journal.path)

    def test_metric_buckets_must_align_and_alarm_config_must_not_change(self):
        """Duplicate/stale buckets and relaxed thresholds cannot satisfy verification."""
        self.plan()
        metrics = self.journal.snapshot["metrics"]
        metrics["values"]["VitalIngestFailures"]["Datapoints"][1]["Timestamp"] = (
            metrics["values"]["VitalIngestFailures"]["Datapoints"][0]["Timestamp"]
        )
        self.assertFalse(demo.Demo.metrics_healthy(metrics, self.cloud.alarms))
        self.runner.apply()
        self.runner.restore()
        self.advance()
        self.cloud.alarms[1]["Threshold"] = 999999
        self.assertFalse(self.runner.restore()["recoveryVerified"])

    def test_image_repository_preserves_registry_port(self):
        """Pinning a maintenance digest works for tagged and digest-only images."""
        self.assertEqual(demo.image_repository("host:5000/repo:tag"), "host:5000/repo")
        self.assertEqual(
            demo.image_repository("host:5000/repo@sha256:abc"), "host:5000/repo"
        )

    def test_failed_rollout_attempts_rollback(self):
        """A failed deployment during apply observation triggers exact restore."""
        self.plan()
        self.cloud.failed_rollout = True
        with self.assertRaisesRegex(RuntimeError, "deployment failed"):
            self.runner.apply(wait_seconds=1)
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertTrue(self.journal.find("restore_result"))

    def test_restore_waits_for_async_rollout_fresh_periods_and_alarm_recovery(self):
        """No update acknowledgement or stale healthy bucket releases ownership."""
        self.plan()
        self.runner.apply()
        self.cloud.async_restore = True
        self.cloud.alarms[0]["StateValue"] = "ALARM"
        clock = self.polling_clock(self.complete_restore_later)
        call_count = len(self.cloud.calls)
        result = self.runner.restore(wait_seconds=300)
        self.assertTrue(result["recoveryVerified"])
        self.assertEqual(result["restorationState"], "verified")
        self.assertFalse(result["waitExpired"])
        self.assertGreaterEqual(clock.elapsed, 180)
        self.assertLess(clock.elapsed, 300)
        self.assertEqual(
            sum(op == "update-service" for _, op, _ in self.cloud.calls[call_count:]), 1
        )
        first = self.journal.find("restore_result")[0]["data"]
        self.assertFalse(first["checks"]["originalRollout"])
        self.assertFalse(first["recoveryVerified"])
        self.assertTrue(self.journal.find("released"))
        self.assertFalse(
            any(tag["key"] == demo.OWNER for tag in self.cloud.service["tags"])
        )

    def test_restore_timeout_retains_pending_state_journal_and_owner(self):
        """Missing observations exhaust the budget without releasing an active run."""
        self.plan()
        self.runner.apply()
        self.cloud.missing_metrics = True
        clock = self.polling_clock()
        result = self.runner.restore(wait_seconds=240)
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(result["restorationState"], "pending")
        self.assertTrue(result["waitExpired"])
        self.assertEqual(clock.elapsed, 240)
        self.assertTrue(
            any(tag["key"] == demo.OWNER for tag in self.cloud.service["tags"])
        )
        self.assertFalse(self.journal.find("released"))
        retained = demo.Journal(self.journal.path).find("restore_wait_result")[-1][
            "data"
        ]
        self.assertEqual(retained["restorationState"], "pending")
        self.cloud.missing_metrics = False
        self.assertTrue(self.runner.restore(wait_seconds=60)["recoveryVerified"])

    def test_workload_mismatch_rejected_without_cloud_mutation(self):
        """A requested profile is a baseline assertion, never a fault-time override."""
        for key in (
            "TRAFFIC_INTERVAL_SECONDS",
            "TRAFFIC_QUERY_LIMIT",
            "DB_OBSERVABILITY_ENABLED",
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(RuntimeError, "baseline setting mismatch"),
            ):
                self.plan(expect_settings={key: "different"})
        self.assertFalse(
            any(
                op
                in {
                    "tag-resource",
                    "register-task-definition",
                    "update-service",
                    "run-task",
                }
                for _, op, _ in self.cloud.calls
            )
        )
        self.assertFalse(self.journal.events)

    def test_environment_guard_rejects_workload_addition_change_and_removal(self):
        """Even a future registration edit cannot silently change baseline load."""
        self.plan()
        original = self.journal.snapshot["taskDefinition"]["taskDefinition"]
        for key, replacement in (
            ("TRAFFIC_QUERY_LIMIT", "99"),
            ("DB_OBSERVABILITY_ENABLED", "true"),
            ("TRAFFIC_SEED", "456"),
            ("TRAFFIC_SEED", None),
        ):
            with self.subTest(key=key, replacement=replacement):
                candidate = copy.deepcopy(original)
                environment = demo.env_map(candidate, "healthcare")
                environment.pop(key, None)
                if replacement is not None:
                    environment[key] = replacement
                candidate["containerDefinitions"][0]["environment"] = [
                    {"name": name, "value": value}
                    for name, value in environment.items()
                ]
                with self.assertRaisesRegex(RuntimeError, "same-load invariant"):
                    self.runner.assert_scenario_environment(candidate)

    def test_baseline_wire_partial_put_and_lost_response(self):
        """Only canonical downloaded content proves an immutable PUT, even with no receipt."""
        self.cloud.put_failure = True
        self.plan()
        self.runner.apply()
        raw = self.cloud.objects[("configured-evidence", "baselines/run-1/normal.json")]
        baseline = json.loads(raw)
        self.assertEqual(raw, demo.canonical(baseline))
        self.assertEqual(baseline["schema_version"], 1)
        self.assertEqual(baseline["scope"]["desired_count"], 1)
        self.assertEqual(set(baseline["service_settings"]), set(demo.SERVICE_SETTINGS))
        self.assertTrue(
            any(
                o["message"]["event"] == "write_contract"
                and o["message"]["schema_name"] is None
                for o in baseline["observations"]
            )
        )
        self.assertEqual(
            set(baseline["metric_observations"]),
            {"start", "end", "attempts", "failures"},
        )
        self.assertEqual(
            {o["message"]["event"] for o in baseline["observations"]},
            {
                "source_manifest",
                "write_contract",
                "write_completed",
                "db_schema_snapshot",
                "write_accounting",
            },
        )
        metadata = json.loads(self.cloud.alarms[0]["AlarmDescription"])
        self.assertEqual(
            metadata["baseline_ref"]["sha256"], hashlib.sha256(raw).hexdigest()
        )
        operations = [op for _, op, _ in self.cloud.calls]
        for _, operation, request in self.cloud.calls:
            if operation == "put-metric-alarm":
                self.assertFalse(
                    {
                        "AlarmArn",
                        "StateValue",
                        "StateReason",
                        "StateUpdatedTimestamp",
                    }.intersection(request)
                )
                self.assertLessEqual(len(request.get("AlarmDescription", "")), 1024)
        self.assertLess(
            operations.index("get-object"), operations.index("put-metric-alarm")
        )
        self.assertLess(
            operations.index("put-metric-alarm"), operations.index("update-service")
        )
        self.assertFalse(
            any(
                op
                in {"set-alarm-state", "put-log-events", "put-metric-data", "run-task"}
                for op in operations
            )
        )

    def test_corrupt_or_existing_s3_object_cannot_start_fault(self):
        """A pre-existing foreign object and a mismatched GET both fail before deployment."""
        self.cloud.objects[("configured-evidence", "baselines/run-1/normal.json")] = (
            b"foreign"
        )
        self.plan()
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            self.runner.apply()
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertFalse(self.journal.find("service_revision"))
        self.assertEqual(
            self.cloud.objects[("configured-evidence", "baselines/run-1/normal.json")],
            b"foreign",
        )

    def test_normal_evidence_freshness_schema_and_safe_fields(self):
        """Missing source/schema/write, changed schema and private payloads stop planning."""
        for missing in (
            "source_manifest",
            "write_completed",
            "db_schema_snapshot",
            "write_contract",
            "write_accounting",
        ):
            self.cloud.omit_event = missing
            with self.subTest(missing=missing), self.assertRaises(RuntimeError):
                self.plan()
        self.cloud.omit_event = None
        self.cloud.bad_schema = True
        with self.assertRaisesRegex(RuntimeError, "column precondition"):
            self.plan()
        self.cloud.bad_schema = False
        self.cloud.unsafe_event = True
        with self.assertRaisesRegex(RuntimeError, "field contract"):
            self.plan()
        self.assertFalse(self.journal.events)

    def test_absent_zero_and_invalid_metrics_never_prove_normal(self):
        """Missing bins are not zero failures and zero attempts are not successful work."""
        self.plan()
        baseline = self.journal.snapshot["metrics"]
        for metric, points in (
            ("VitalIngestFailures", []),
            ("VitalIngestAttempts", []),
        ):
            candidate = copy.deepcopy(baseline)
            candidate["values"][metric]["Datapoints"] = points
            self.assertFalse(demo.Demo.metrics_healthy(candidate, []))
        for value in (0, False, float("nan"), -1):
            candidate = copy.deepcopy(baseline)
            candidate["values"]["VitalIngestAttempts"]["Datapoints"][0]["Sum"] = value
            self.assertFalse(demo.Demo.metrics_healthy(candidate, []))

    def test_original_alarm_metadata_and_criteria_restored(self):
        """Cleanup restores the original Unicode description and every metric/action setting."""
        self.cloud.alarms[0].update(
            AlarmDescription="원래 설명", InsufficientDataActions=["arn:action"]
        )
        original = copy.deepcopy(self.cloud.alarms[0])
        self.plan()
        self.runner.apply()
        self.runner.restore()
        self.assertEqual(self.cloud.alarms[0], original)
        self.advance()
        self.assertTrue(self.runner.restore()["recoveryVerified"])
        self.assertIn("원래 설명", demo.canonical(original).decode())
        with self.assertRaises(ValueError):
            demo.canonical({"bad": float("nan")})

    def test_interrupted_apply_and_journal_loss_cleanup_independently(self):
        """SIGINT after service mutation still restores service and alarm with a failed journal."""
        self.plan()
        original_call = self.cloud.__call__
        original_append = self.journal.append
        interrupted = False

        def call(service, operation, /, **payload):
            nonlocal interrupted
            result = original_call(service, operation, **payload)
            if (
                operation == "update-service"
                and payload["taskDefinition"] != self.cloud.original
                and not interrupted
            ):
                interrupted = True
                self.journal.append = lambda *args: (_ for _ in ()).throw(
                    OSError("disk full")
                )
                raise KeyboardInterrupt("interrupt after mutation")
            return result

        self.runner.aws = call
        with self.assertRaises(KeyboardInterrupt):
            self.runner.apply()
        self.journal.append = original_append
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertEqual(self.cloud.alarms[0].get("AlarmDescription", ""), "")
        self.assertFalse(self.runner.cleanup_result["recoveryVerified"])

    def test_foreign_service_still_allows_owned_alarm_cleanup(self):
        """Independent metadata cleanup never authorizes overwriting a foreign deployment."""
        self.plan()
        self.runner.apply()
        self.cloud.service["taskDefinition"] = "foreign"
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(self.cloud.service["taskDefinition"], "foreign")
        self.assertEqual(self.cloud.alarms[0].get("AlarmDescription", ""), "")

    def test_foreign_alarm_refused_while_service_restored(self):
        """Foreign metadata survives cleanup; failure cannot prevent our service rollback."""
        self.plan()
        self.runner.apply()
        self.cloud.alarms[0]["AlarmDescription"] = "someone else's incident"
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertEqual(
            self.cloud.alarms[0]["AlarmDescription"], "someone else's incident"
        )

    def test_legacy_journal_remains_byte_identical(self):
        """Old hash chains load unchanged but cannot be repurposed as new scenario runs."""
        self.journal.append(
            "snapshot",
            {"target": self.cloud.target, "options": {"scenario": "maintenance-lock"}},
        )
        original = (self.journal.path / "000000.json").read_bytes()
        self.runner.journal = demo.Journal(self.journal.path)
        for action in (self.runner.apply, self.runner.status, self.runner.restore):
            with self.assertRaisesRegex(RuntimeError, "legacy journal"):
                action()
        self.assertEqual((self.journal.path / "000000.json").read_bytes(), original)
        self.assertEqual(len(list(self.journal.path.glob("*.json"))), 1)

    def test_cli_adapter_get_object_uses_explicit_output_file(self):
        """AWS CLI's positional output path must not leak into GetObject API input."""
        with patch.object(
            demo.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "{}", ""),
        ) as run:
            demo.Aws("us-east-1")(
                "s3api",
                "get-object",
                Bucket="bucket",
                Key="key",
                OutputFile="/tmp/test-output",
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "/tmp/test-output")
        self.assertEqual(argv[argv.index("--bucket") + 1], "bucket")
        self.assertEqual(argv[argv.index("--key") + 1], "key")
        self.assertNotIn("--cli-input-json", argv)

    def test_cli_put_object_loads_file_bytes_instead_of_decoding_path_as_base64(self):
        """A baseline filename is a CLI file input, never an API blob value."""
        with patch.object(
            demo.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "{}", ""),
        ) as run:
            demo.Aws("us-east-1")(
                "s3api",
                "put-object",
                Bucket="bucket",
                Key="key",
                Body="/tmp/normal baseline.json",
                IfNoneMatch="*",
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--body") + 1], "/tmp/normal baseline.json")
        self.assertEqual(argv[argv.index("--bucket") + 1], "bucket")
        self.assertEqual(argv[argv.index("--key") + 1], "key")
        payload = json.loads(argv[argv.index("--cli-input-json") + 1])
        self.assertNotIn("Body", payload)
        self.assertEqual(payload["IfNoneMatch"], "*")

    def test_optional_capacity_absence_is_canonical_without_dropping_circuit_breaker_fields(
        self,
    ):
        """Serialization differences are equivalent; real control changes remain different."""
        service = copy.deepcopy(self.cloud.service)
        service.pop("capacityProviderStrategy", None)
        service["deploymentConfiguration"]["deploymentCircuitBreaker"] = {
            "enable": True,
            "rollback": True,
            "resetOnHealthyTask": True,
            "thresholdConfiguration": {"type": "BOUNDED_PERCENT", "value": 50},
        }
        absent = copy.deepcopy(demo.settings(service))
        service["capacityProviderStrategy"] = None
        self.assertEqual(absent, demo.settings(service))
        service["capacityProviderStrategy"] = []
        self.assertEqual(absent, demo.settings(service))
        service["deploymentConfiguration"]["deploymentCircuitBreaker"][
            "resetOnHealthyTask"
        ] = False
        self.assertNotEqual(absent, demo.settings(service))

    def test_cli_ecs_uses_an_uncompressed_locked_model_without_changing_global_environment(
        self,
    ):
        """Old CLI loaders must see configurable fields instead of silently omitting them."""
        import gzip
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "botocore"
            model_dir = package / "data/ecs/2014-11-13"
            model_dir.mkdir(parents=True)
            model = {
                "metadata": {"endpointPrefix": "ecs"},
                "shapes": {
                    "DeploymentCircuitBreaker": {
                        "members": {
                            "resetOnHealthyTask": {"shape": "Boolean"},
                            "thresholdConfiguration": {"shape": "Threshold"},
                        }
                    }
                },
            }
            (model_dir / "service-2.json.gz").write_bytes(
                gzip.compress(json.dumps(model).encode())
            )
            original = os.environ.get("AWS_DATA_PATH")
            with patch.dict(
                sys.modules,
                {"botocore": SimpleNamespace(__file__=str(package / "__init__.py"))},
            ):
                adapter = demo.Aws("us-east-1")
                environment = adapter._environment("ecs")
                path = Path(environment["AWS_DATA_PATH"].split(os.pathsep)[0])
                self.assertEqual(
                    json.loads((path / "ecs/2014-11-13/service-2.json").read_text()),
                    model,
                )
                self.assertEqual(environment, adapter._environment("ecs"))
                self.assertEqual(os.environ.get("AWS_DATA_PATH"), original)
                adapter._model_directory.cleanup()

    def test_cli_lock_and_unreleased_prior_run_block_plan(self):
        """Name/ARN aliases resolve to the same lock and prior journals prevent overlap."""
        directory = Path(self.temp.name) / demo.digest(self.cloud.target)
        directory.mkdir()
        arguments = [
            "plan",
            "--run-id",
            "new-run",
            "--cluster",
            "alias",
            "--service",
            "alias",
            "--region",
            "us-east-1",
            "--journal-root",
            self.temp.name,
            "--image",
            "repo@sha256:" + "b" * 64,
            "--evidence-bucket",
            "configured-evidence",
            "--alarm",
            "ingest",
        ]
        with patch.object(demo, "Aws", return_value=self.cloud):
            with (directory / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "owns this service lock"):
                    demo.main(arguments)
            old = demo.Journal(directory / "old-run")
            old.append("snapshot", {"historical": True})
            with self.assertRaisesRegex(RuntimeError, "unrestored local RunId"):
                demo.main(arguments)
        self.assertFalse(any(op == "tag-resource" for _, op, _ in self.cloud.calls))

    def test_lost_alarm_response_still_restores_original_description(self):
        """An interrupted metadata request remains owned by its persisted intent."""
        self.plan()
        call = self.cloud.__call__
        injected = False

        def lost(service, operation, /, **payload):
            nonlocal injected
            result = call(service, operation, **payload)
            if operation == "put-metric-alarm" and not injected:
                injected = True
                raise RuntimeError("lost alarm update response")
            return result

        self.runner.aws = lost
        with self.assertRaisesRegex(RuntimeError, "lost alarm"):
            self.runner.apply()
        self.assertNotIn("AlarmDescription", self.cloud.alarms[0])
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertFalse(self.journal.find("service_revision"))

    def test_stale_normal_schema_and_write_events_refuse_plan(self):
        """Events fetched from the right stream still need actual fresh timestamps."""
        call = self.cloud.__call__

        def stale(service, operation, /, **payload):
            result = call(service, operation, **payload)
            if (
                operation == "filter-log-events"
                and "db_schema_snapshot" in payload["filterPattern"]
            ):
                for event in result["events"]:
                    event["timestamp"] -= 180000
                    message = json.loads(event["message"])
                    message["observed_at"] = datetime.fromtimestamp(
                        event["timestamp"] / 1000, timezone.utc
                    ).isoformat()
                    event["message"] = json.dumps(message)
            return result

        self.runner.aws = stale
        with self.assertRaisesRegex(RuntimeError, "fresh schema"):
            self.plan()
        self.assertFalse(self.journal.events)

    def test_foreign_service_settings_between_plan_and_apply_refuse_writes(self):
        """A valid snapshot cannot authorize changed traffic/service settings."""
        self.plan()
        self.cloud.service["desiredCount"] = 2
        count = len(self.cloud.calls)
        with self.assertRaisesRegex(RuntimeError, "dirty service settings"):
            self.runner.apply()
        self.assertFalse(
            any(
                op
                in {"tag-resource", "put-object", "put-metric-alarm", "update-service"}
                for _, op, _ in self.cloud.calls[count:]
            )
        )

    def test_interrupt_during_s3_put_never_continues_to_fault(self):
        """A signal remains an abort even when the immutable object reached S3."""
        self.plan()
        call = self.cloud.__call__

        def interrupted_put(service, operation, /, **payload):
            result = call(service, operation, **payload)
            if operation == "put-object":
                raise KeyboardInterrupt("operator interrupted")
            return result

        self.runner.aws = interrupted_put
        with self.assertRaises(KeyboardInterrupt):
            self.runner.apply()
        self.assertFalse(self.journal.find("service_revision"))
        self.assertFalse(self.journal.find("alarm_decoration_intent"))
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)


if __name__ == "__main__":
    unittest.main()
