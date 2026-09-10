"""Deterministic cloud model for the realistic demo orchestration contract."""

import ast
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import io
import json
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
        files = {"revision/query.py": "1" * 64, "revision/session.py": "2" * 64}
        self.source_manifest = {
            "event": "source_manifest",
            "revision": "r1",
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
                "createdAt": Clock.current.isoformat(),
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
            result = {"MetricAlarms": self.alarms}
        elif operation == "filter-log-events":
            result = {
                "events": []
                if self.source_events_missing
                else [
                    {
                        "eventId": "manifest-1",
                        "logStreamName": payload["logStreamNames"][0],
                        "timestamp": payload["startTime"] + 1000,
                        "message": json.dumps(self.source_manifest),
                    }
                ]
            }
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
        self.cloud = Cloud()
        self.journal = demo.Journal(Path(self.temp.name) / "run-1")
        self.runner = demo.Demo(self.cloud, self.journal, self.cloud.target)
        Clock.current = datetime(2026, 9, 10, 12, 0, 5, tzinfo=timezone.utc)
        self.old_datetime = demo.datetime
        demo.datetime = Clock
        self.addCleanup(setattr, demo, "datetime", self.old_datetime)

    def plan(self, scenario="pool-config", **changes):
        """Freeze caller-supplied controls with deterministic healthy evidence."""
        options = {
            "run_id": "run-1",
            "scenario": scenario,
            "container": "healthcare",
            "image": None,
            "revision": None,
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

    def test_post_mutation_journal_loss_still_restores_service_or_task(self):
        """Successful UpdateService/RunTask must be undone when all later writes fail."""
        for scenario, event in (
            ("pool-config", "update_response"),
            ("maintenance-lock", "maintenance_tasks"),
        ):
            with self.subTest(scenario=scenario):
                self.setUp()
                self.plan(scenario)
                self.cloud.jobs["arn:task/foreign"] = {
                    "taskArn": "arn:task/foreign",
                    "startedBy": "foreign",
                    "lastStatus": "RUNNING",
                }
                with (
                    self.failing_journal(event, persistent=True) as (attempts, stderr),
                    self.assertRaisesRegex(OSError, "storage failure"),
                ):
                    self.runner.apply()
                self.assertIn("apply_error", attempts)
                self.assertIn("restore_service_observation", attempts)
                self.assertIn("maintenance_before_restore", attempts)
                self.assertIn("restore_wait_result", attempts)
                self.assertEqual(
                    self.cloud.service["taskDefinition"], self.cloud.original
                )
                if scenario == "maintenance-lock":
                    self.assertEqual(
                        self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED"
                    )
                    self.assertFalse(self.journal.find("maintenance_tasks"))
                self.assertEqual(
                    self.cloud.jobs["arn:task/foreign"]["lastStatus"], "RUNNING"
                )
                self.assert_journal_failure_retains_owner(
                    self.runner.cleanup_result, stderr
                )
                self.assertFalse(
                    any(op == "untag-resource" for _, op, _ in self.cloud.calls)
                )

    def test_lost_responses_and_failed_error_log_use_persisted_original_intent(self):
        """Rediscover unacknowledged changes from disk even with unavailable error logs."""
        for scenario, flag in (
            ("pool-config", "fail_update"),
            ("maintenance-lock", "lost_run_response"),
        ):
            with self.subTest(scenario=scenario):
                self.setUp()
                self.plan(scenario)
                setattr(self.cloud, flag, True)
                with self.failing_journal("apply_error", persistent=True) as (
                    _,
                    stderr,
                ):
                    with self.assertRaisesRegex(RuntimeError, "response lost"):
                        self.runner.apply()
                    # A new controller has only original durable events, not cached tasks.
                    resumed = demo.Demo(
                        self.cloud, demo.Journal(self.journal.path), self.cloud.target
                    )
                    if scenario == "maintenance-lock":
                        # Recreate a still-live owned launch for the new controller.
                        self.cloud.jobs["arn:task/owned"]["lastStatus"] = "RUNNING"
                        self.cloud.jobs["arn:task/owned"]["tags"] = [
                            {"key": k, "value": v}
                            for k, v in resumed.owner_tags().items()
                        ]
                    result = resumed.restore()
                self.assertEqual(
                    self.cloud.service["taskDefinition"], self.cloud.original
                )
                if scenario == "maintenance-lock":
                    self.assertEqual(
                        self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED"
                    )
                    self.assertFalse(self.journal.find("maintenance_tasks"))
                    self.assertEqual(
                        sum(op == "run-task" for _, op, _ in self.cloud.calls), 1
                    )
                self.assert_journal_failure_retains_owner(result, stderr)

    def test_post_apply_status_write_failure_triggers_cleanup(self):
        """A failed observation after a successful mutation still invokes compensation."""
        for scenario, response in (
            ("pool-config", "update_response"),
            ("maintenance-lock", "maintenance_tasks"),
        ):
            with self.subTest(scenario=scenario):
                self.setUp()
                self.plan(scenario)
                with (
                    self.failing_journal("status", after=response) as (_, stderr),
                    self.assertRaises(OSError),
                ):
                    self.runner.apply()
                self.assertEqual(
                    self.cloud.service["taskDefinition"], self.cloud.original
                )
                if scenario == "maintenance-lock":
                    self.assertEqual(
                        self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED"
                    )
                self.assert_journal_failure_retains_owner(
                    self.runner.cleanup_result, stderr
                )

    def test_journal_and_diagnostic_failure_still_restore_owned_resources(self):
        """A broken or closed stderr cannot replace the original error or block cleanup."""
        for scenario, event in (
            ("pool-config", "update_response"),
            ("maintenance-lock", "maintenance_tasks"),
        ):
            for error_type in (OSError, ValueError):
                with self.subTest(scenario=scenario, diagnostic=error_type.__name__):
                    self.setUp()
                    self.plan(scenario)
                    self.cloud.jobs["arn:task/foreign"] = {
                        "taskArn": "arn:task/foreign",
                        "startedBy": "foreign",
                        "lastStatus": "RUNNING",
                    }
                    with (
                        self.failing_journal(event, persistent=True) as (_, stderr),
                        patch.object(
                            stderr,
                            "write",
                            side_effect=error_type("unavailable diagnostic"),
                        ),
                        self.assertRaisesRegex(OSError, "storage failure"),
                    ):
                        self.runner.apply()
                    result = self.runner.cleanup_result
                    self.assertFalse(result["recoveryVerified"])
                    self.assertEqual(result["restorationState"], "pending")
                    self.assertTrue(result["journalErrors"])
                    self.assertTrue(
                        all(
                            item["diagnosticErrorType"] == error_type.__name__
                            for item in result["journalErrors"]
                        )
                    )
                    self.assertEqual(
                        self.cloud.service["taskDefinition"], self.cloud.original
                    )
                    if scenario == "maintenance-lock":
                        self.assertEqual(
                            self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED"
                        )
                    self.assertEqual(
                        self.cloud.jobs["arn:task/foreign"]["lastStatus"], "RUNNING"
                    )
                    self.assertFalse(self.journal.find("released"))
                    tags = {t["key"]: t["value"] for t in self.cloud.service["tags"]}
                    for key, value in self.runner.owner_tags().items():
                        self.assertEqual(tags[key], value)

    def test_recovery_journal_boundaries_do_not_block_either_cleanup(self):
        """Each recovery write failure preserves both compensations and foreign tasks."""
        for event in (
            "restore_intent",
            "restore_service_observation",
            "restore_update_intent",
            "restore_update_response",
            "owned_tasks",
            "maintenance_before_restore",
            "stop_intent",
            "stop_response",
            "restored_rollout_observed",
            "status",
            "restore_result",
            "restore_wait_result",
        ):
            with self.subTest(event=event):
                self.setUp()
                self.plan("maintenance-lock")
                self.runner.apply()
                self.add_owned_service_change()
                self.cloud.jobs["arn:task/foreign"] = {
                    "taskArn": "arn:task/foreign",
                    "startedBy": "foreign",
                    "lastStatus": "RUNNING",
                }
                with self.failing_journal(event) as (attempts, stderr):
                    result = self.runner.restore()
                self.assertIn(event, attempts)
                self.assertEqual(
                    self.cloud.service["taskDefinition"], self.cloud.original
                )
                self.assertEqual(
                    self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED"
                )
                self.assertEqual(
                    self.cloud.jobs["arn:task/foreign"]["lastStatus"], "RUNNING"
                )
                self.assert_journal_failure_retains_owner(result, stderr)
                self.assertFalse(
                    any(op == "untag-resource" for _, op, _ in self.cloud.calls)
                )

    def test_failed_task_cleanup_log_does_not_skip_later_owned_tasks(self):
        """A failed stop log for one task cannot prevent stopping the next owned task."""
        self.plan("maintenance-lock")
        self.runner.apply()
        extra = copy.deepcopy(self.cloud.jobs["arn:task/owned"])
        extra["taskArn"] = "arn:task/owned-second"
        self.cloud.jobs[extra["taskArn"]] = extra
        with self.failing_journal("stop_response", persistent=True) as (_, stderr):
            result = self.runner.restore()
        self.assertTrue(
            all(t["lastStatus"] == "STOPPED" for t in self.cloud.jobs.values())
        )
        self.assert_journal_failure_retains_owner(result, stderr)

    def test_journal_loss_preserves_foreign_service_and_task_guards(self):
        """Storage failure cannot authorize a foreign owner, deployment, setting or task."""
        for foreign in (
            "owner",
            "deployment",
            "settings",
            "task-tags",
            "task-definition",
        ):
            with self.subTest(foreign=foreign):
                self.setUp()
                self.plan("maintenance-lock")
                self.runner.apply()
                self.add_owned_service_change()
                if foreign == "owner":
                    self.cloud.service["tags"] = [{"key": demo.OWNER, "value": "peer"}]
                elif foreign == "deployment":
                    self.cloud.service["deployments"][0]["taskDefinition"] = "foreign"
                elif foreign == "settings":
                    self.cloud.service["desiredCount"] = 2
                elif foreign == "task-tags":
                    self.cloud.jobs["arn:task/owned"]["tags"] = []
                else:
                    self.cloud.jobs["arn:task/owned"]["taskDefinitionArn"] = "foreign"
                before = copy.deepcopy(self.cloud.service)
                with self.failing_journal("restore_intent", persistent=True):
                    result = self.runner.restore()
                self.assertFalse(result["recoveryVerified"])
                if foreign.startswith("task-"):
                    self.assertEqual(
                        self.cloud.service["taskDefinition"], self.cloud.original
                    )
                    self.assertEqual(
                        self.cloud.jobs["arn:task/owned"]["lastStatus"], "RUNNING"
                    )
                else:
                    self.assertEqual(self.cloud.service, before)
                    self.assertEqual(
                        self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED"
                    )

    def test_new_mutations_require_successful_prechange_writes(self):
        """Failed intent persistence prevents the corresponding new external mutation."""
        for scenario, event, forbidden in (
            ("pool-config", "apply_intent", "tag-resource"),
            ("pool-config", "register_intent", "register-task-definition"),
            ("pool-config", "update_intent", "update-service"),
            ("maintenance-lock", "run_task_intent", "run-task"),
        ):
            with self.subTest(event=event):
                self.setUp()
                self.plan(scenario)
                with (
                    self.failing_journal(event, persistent=True),
                    self.assertRaises(OSError),
                ):
                    self.runner.apply()
                self.assertFalse(any(op == forbidden for _, op, _ in self.cloud.calls))
                self.assertFalse(demo.Journal(self.journal.path).find(event))

    def test_unjournaled_rollback_requires_prior_durable_update_intent(self):
        """A known revision alone cannot justify rollback when its new intent cannot save."""
        self.plan()
        self.runner.apply()
        # Model a legacy journal that knows the revision but lacks UpdateService intent.
        events = [e for e in self.journal.events if e["kind"] != "update_intent"]
        journal = demo.Journal(Path(self.temp.name) / "legacy-no-update")
        for event in events:
            journal.append(event["kind"], event["data"])
        runner = demo.Demo(self.cloud, journal, self.cloud.target)
        self.cloud.service["tags"] = [
            {"key": k, "value": v} for k, v in runner.owner_tags().items()
        ]
        before = self.cloud.service["taskDefinition"]
        with self.failing_journal("restore_update_intent"):
            result = runner.restore()
        self.assertEqual(self.cloud.service["taskDefinition"], before)
        self.assertIn("no persisted update intent", result["errors"][0]["error"])

    def test_journal_loss_does_not_bypass_original_image_guard(self):
        """A legacy mutable original stays refused while owned task cleanup continues."""
        self.plan("maintenance-lock")
        self.runner.apply()
        self.add_owned_service_change()
        snapshot = self.journal.snapshot
        snapshot["taskDefinition"]["taskDefinition"]["containerDefinitions"][0][
            "image"
        ] = "repo:latest"
        journal = demo.Journal(Path(self.temp.name) / "legacy-mutable")
        journal.append("snapshot", snapshot)
        for event in self.journal.events[1:]:
            # Legacy journals predate ownership receipts; do not transplant proof
            # bound to the modern snapshot into this different legacy snapshot.
            if event["kind"] != "task_ownership_verified":
                journal.append(event["kind"], event["data"])
        runner = demo.Demo(self.cloud, journal, self.cloud.target)
        tags = [{"key": k, "value": v} for k, v in runner.owner_tags().items()]
        self.cloud.service["tags"] = tags
        self.cloud.jobs["arn:task/owned"].update(tags=tags, startedBy=runner.token())
        before = self.cloud.service["taskDefinition"]
        with self.failing_journal("restore_intent", persistent=True):
            result = runner.restore()
        self.assertEqual(self.cloud.service["taskDefinition"], before)
        self.assertEqual(self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED")
        self.assertFalse(result["recoveryVerified"])

    def test_cli_journal_loss_returns_failure_and_in_memory_cleanup(self):
        """CLI output includes recovery errors even when final journal records cannot save."""
        for action, scenario, event in (
            ("apply", "pool-config", "update_response"),
            ("apply", "maintenance-lock", "maintenance_tasks"),
            ("restore", "pool-config", "restore_intent"),
            ("restore", "maintenance-lock", "owned_tasks"),
        ):
            with self.subTest(action=action, scenario=scenario):
                self.setUp()
                common = self.connect_cli()
                self.plan(scenario)
                if action == "restore":
                    self.runner.apply()
                stdout = io.StringIO()
                with (
                    self.failing_journal(event, persistent=True) as (_, stderr),
                    contextlib.redirect_stdout(stdout),
                ):
                    code = demo.main([action, *common])
                self.assertEqual(code, 1 if action == "apply" else 2)
                if action == "apply":
                    decoder = json.JSONDecoder()
                    text = stderr.getvalue().strip()
                    messages = []
                    while text:
                        message, offset = decoder.raw_decode(text)
                        messages.append(message)
                        text = text[offset:].lstrip()
                    result = messages[-1]["cleanup"]
                    self.assertFalse(messages[-1]["operationSucceeded"])
                else:
                    result = json.loads(stdout.getvalue())
                self.assert_journal_failure_retains_owner(result, stderr)
                self.assertEqual(
                    self.cloud.service["taskDefinition"], self.cloud.original
                )
                if scenario == "maintenance-lock":
                    self.assertEqual(
                        self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED"
                    )

    def test_healthy_recovery_does_not_release_before_all_evidence_saves(self):
        """Even healthy metrics cannot allow release after a late recovery write failure."""
        for event in (
            "status",
            "restore_result",
            "restore_wait_result",
            "release_intent",
            "released",
        ):
            with self.subTest(event=event):
                self.setUp()
                self.plan()
                self.runner.apply()
                self.runner.restore()
                self.advance()
                with self.failing_journal(event) as (_, stderr):
                    result = self.runner.restore()
                self.assertTrue(all(result["checks"].values()))
                self.assert_journal_failure_retains_owner(result, stderr)
                if event != "released":
                    self.assertFalse(
                        any(op == "untag-resource" for _, op, _ in self.cloud.calls)
                    )

    def test_journal_failure_stays_unverified_after_writes_resume_in_same_command(self):
        """A transient evidence gap cannot be erased by later successful polls or logs."""
        self.plan()
        self.runner.apply()
        with self.failing_journal("restore_intent"):
            self.runner.restore()
        self.runner.restore()
        self.advance()
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertTrue(result["journalErrors"])
        self.assertFalse(any(op == "untag-resource" for _, op, _ in self.cloud.calls))

    def test_persistent_journal_loss_keeps_retrying_both_cleanup_branches(self):
        """The wait budget still retries a transient task failure while storage is down."""
        self.plan("maintenance-lock")
        self.runner.apply()
        self.add_owned_service_change()
        self.cloud.fail_stop = True
        self.polling_clock(lambda elapsed: setattr(self.cloud, "fail_stop", False))
        with self.failing_journal("restore_intent", persistent=True) as (_, stderr):
            result = self.runner.restore(wait_seconds=30)
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertEqual(self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED")
        self.assertGreaterEqual(
            sum(op == "stop-task" for _, op, _ in self.cloud.calls), 2
        )
        self.assert_journal_failure_retains_owner(result, stderr)
        self.assertTrue(result["waitExpired"])

    def test_lost_release_log_does_not_reclaim_foreign_ownership(self):
        """A peer claiming after release cannot be overwritten to repair our journal gap."""
        self.plan()
        self.runner.apply()
        self.runner.restore()
        self.advance()
        original = self.journal.append

        def append(kind, data):
            """Model a peer claim exactly when the release acknowledgement cannot save."""
            if kind == "released":
                self.cloud.service["tags"] = [{"key": demo.OWNER, "value": "peer"}]
                raise OSError("release journal unavailable")
            return original(kind, data)

        with (
            patch.object(self.journal, "append", append),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(
            self.cloud.service["tags"], [{"key": demo.OWNER, "value": "peer"}]
        )
        self.assertEqual(sum(op == "tag-resource" for _, op, _ in self.cloud.calls), 1)

    def test_cleanup_error_logging_cannot_replace_original_apply_failure(self):
        """A second failed diagnostic append must leave the original error visible."""
        self.plan()
        self.cloud.fail_update = True
        with (
            patch.object(
                self.runner, "restore", side_effect=RuntimeError("cleanup failed")
            ),
            self.failing_journal("cleanup_error") as (attempts, stderr),
            self.assertRaisesRegex(RuntimeError, "update response lost"),
        ):
            self.runner.apply()
        self.assertIn("cleanup_error", attempts)
        self.assertIn("journal append failed", stderr.getvalue())

    def test_pool_round_trip_preserves_original_definition_absence_and_tags(self):
        """Restore the exact ARN and absent timeout rather than reconstructed defaults."""
        self.plan()
        snapshot_bytes = (self.journal.path / "000000.json").read_bytes()
        self.assertEqual(
            self.journal.snapshot["environment"]["DB_POOL_TIMEOUT_SECONDS"],
            {"present": False, "value": None},
        )
        self.assertFalse(
            any(
                op in {"update-service", "register-task-definition", "tag-resource"}
                for _, op, _ in self.cloud.calls
            )
        )
        result = self.runner.apply()
        self.assertFalse(result["scenarioSuccess"])
        active = self.cloud.definitions[self.cloud.service["taskDefinition"]][
            "taskDefinition"
        ]
        env = demo.env_map(active, "healthcare")
        self.assertEqual(
            (
                env["DB_POOL_SIZE"],
                env["DB_MAX_OVERFLOW"],
                env["DB_POOL_TIMEOUT_SECONDS"],
            ),
            ("1", "0", "0.25"),
        )
        self.assertEqual(env["TRAFFIC_SEED"], "123")
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.advance()
        self.assertTrue(self.runner.restore()["recoveryVerified"])
        self.assertEqual(
            self.cloud.service["tags"], [{"key": "Project", "value": "keep"}]
        )
        self.assertEqual(
            (self.journal.path / "000000.json").read_bytes(), snapshot_bytes
        )
        self.assertTrue(demo.Journal(self.journal.path).find("released"))

    def test_revision_scenarios_use_caller_digest_and_source_revision(self):
        """Query and session revisions never substitute runtime fault flags."""
        for scenario in ("query-revision", "session-revision"):
            with self.subTest(scenario=scenario):
                self.setUp()
                image = "registry.example/healthcare@sha256:" + "b" * 64
                self.plan(scenario, image=image, revision="caller-source-r2")
                self.runner.apply()
                active = self.cloud.definitions[self.cloud.service["taskDefinition"]][
                    "taskDefinition"
                ]
                self.assertEqual(active["containerDefinitions"][0]["image"], image)
                env = demo.env_map(active, "healthcare")
                self.assertEqual(env["DEPLOYED_REVISION"], "caller-source-r2")
                self.assertFalse(any(key.startswith("FAULT_") for key in env))
                self.runner.restore()
                self.advance()
                self.assertTrue(self.runner.restore()["recoveryVerified"])

    def test_maintenance_is_separate_same_digest_and_only_owned_task_stops(self):
        """Maintenance uses the healthy image and network without the web healthcheck."""
        self.plan("maintenance-lock")
        self.cloud.jobs["arn:task/foreign"] = {
            "taskArn": "arn:task/foreign",
            "startedBy": "foreign",
            "lastStatus": "RUNNING",
        }
        self.runner.apply()
        request = next(p for _, op, p in self.cloud.calls if op == "run-task")
        td = self.cloud.definitions[request["taskDefinition"]]["taskDefinition"]
        app = td["containerDefinitions"][0]
        self.assertIn("-demo-maint-", td["family"])
        self.assertEqual(len(td["containerDefinitions"]), 1)
        self.assertEqual(
            app["image"], "registry.example/healthcare@" + self.cloud.healthy_digest
        )
        self.assertEqual(app["entryPoint"], ["python"])
        self.assertEqual(
            app["command"],
            [
                "-m",
                "test_service.maintenance",
                "--run-id",
                "run-1",
                "--hold-seconds",
                "180",
                "--schema",
                "public",
            ],
        )
        self.assertNotIn("healthCheck", app)
        self.assertNotIn("dependsOn", app)
        self.assertEqual(
            request["networkConfiguration"],
            self.journal.snapshot["service"]["networkConfiguration"],
        )
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.runner.restore()
        self.advance()
        self.assertTrue(self.runner.restore()["recoveryVerified"])
        self.assertEqual(self.cloud.jobs["arn:task/foreign"]["lastStatus"], "RUNNING")
        self.assertEqual(
            [p["task"] for _, op, p in self.cloud.calls if op == "stop-task"],
            ["arn:task/owned"],
        )

    def test_legacy_arbitrary_command_refuses_apply_but_restores_owned_task(self):
        """Reject old launch controls while retaining lost-response ownership cleanup."""
        command = ["python", "-c", "pass", "{run_id}", "{hold_seconds}"]
        self.plan("maintenance-lock", maintenance_command=command)
        snapshot_bytes = (self.journal.path / "000000.json").read_bytes()
        self.cloud.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "unsupported maintenance command"):
            self.runner.apply()
        with self.assertRaisesRegex(RuntimeError, "unsupported maintenance command"):
            self.runner.register(maintenance=True)
        self.assertEqual(self.cloud.calls, [])
        self.assertFalse(self.journal.find("apply_intent"))

        # Recreate an old owned launch whose RunTask response was never recorded.
        arn = "arn:task-definition/legacy-maintenance:1"
        self.journal.append("maintenance_revision", {"arn": arn})
        self.journal.append("run_task_intent", {"taskDefinition": arn})
        tags = [{"key": k, "value": v} for k, v in self.runner.owner_tags().items()]
        self.cloud.service["tags"] = tags
        self.cloud.jobs["arn:task/owned"] = {
            "taskArn": "arn:task/owned",
            "taskDefinitionArn": arn,
            "startedBy": self.runner.token(),
            "lastStatus": "RUNNING",
            "tags": tags,
        }
        self.cloud.jobs["arn:task/foreign"] = {
            "taskArn": "arn:task/foreign",
            "startedBy": "foreign",
            "lastStatus": "RUNNING",
        }
        self.runner.restore()
        self.advance()
        self.assertTrue(self.runner.restore()["recoveryVerified"])
        self.assertEqual(self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED")
        self.assertEqual(self.cloud.jobs["arn:task/foreign"]["lastStatus"], "RUNNING")
        self.assertFalse(
            any(
                op in ("register-task-definition", "run-task")
                for _, op, _ in self.cloud.calls
            )
        )
        self.assertEqual(
            (self.journal.path / "000000.json").read_bytes(), snapshot_bytes
        )

    def test_maintenance_journal_values_are_validated_before_launch(self):
        """Persisted controls cannot bypass the CLI's run, duration or schema bounds."""
        for changes in (
            {"run_id": "bad/run"},
            {"hold_seconds": 0},
            {"hold_seconds": 7200.5},
            {"hold_seconds": float("nan")},
            {"hold_seconds": float("inf")},
            {"hold_seconds": "180"},
            {"hold_seconds": True},
            {"maintenance_schema": "public; SELECT 1"},
        ):
            with self.subTest(changes=changes):
                self.setUp()
                self.plan("maintenance-lock", **changes)
                self.cloud.calls.clear()
                with self.assertRaises(ValueError):
                    self.runner.apply()
                with self.assertRaises(ValueError):
                    self.runner.register(maintenance=True)
                self.assertEqual(self.cloud.calls, [])

    def test_lost_update_response_attempts_exact_rollback(self):
        """An update transport failure still rolls back the already changed service."""
        self.plan()
        self.cloud.fail_update = True
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.runner.apply()
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertTrue(self.journal.find("apply_error"))
        self.assertTrue(self.journal.find("restore_result"))

    def test_lost_run_response_is_rediscovered_by_proven_ownership(self):
        """A created task with an unacknowledged response is safely discovered."""
        self.plan("maintenance-lock")
        self.cloud.lost_run_response = True
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.runner.apply()
        self.assertEqual(self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED")
        self.assertEqual(sum(op == "run-task" for _, op, _ in self.cloud.calls), 1)
        self.assertEqual(self.cloud.jobs["arn:task/owned"]["tags"], [])
        self.assertTrue(self.journal.find("task_ownership_verified"))
        self.advance()
        resumed = demo.Demo(
            self.cloud, demo.Journal(self.journal.path), self.cloud.target
        )
        self.assertTrue(resumed.restore()["recoveryVerified"])

    def test_foreign_deployment_refused_but_owned_maintenance_still_cleaned(self):
        """One failed cleanup path must not skip an independent owned task."""
        self.plan("maintenance-lock")
        self.runner.apply()
        self.cloud.service["networkConfiguration"]["awsvpcConfiguration"]["subnets"] = [
            "foreign"
        ]
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(result["errors"][0]["step"], "service")
        self.assertEqual(self.cloud.jobs["arn:task/owned"]["lastStatus"], "STOPPED")
        self.assertFalse(any(op == "update-service" for _, op, _ in self.cloud.calls))

    def test_foreign_task_tags_cannot_authorize_stop(self):
        """A matching startedBy alone is insufficient task ownership proof."""
        self.plan("maintenance-lock")
        self.runner.apply()
        self.cloud.jobs["arn:task/owned"]["tags"] = [
            {"key": demo.OWNER, "value": "foreign"}
        ]
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertFalse(any(op == "stop-task" for _, op, _ in self.cloud.calls))

    def test_maintenance_stop_failure_keeps_service_cleanup_and_evidence(self):
        """A failed stop retains ownership and independently verifies service state."""
        self.plan("maintenance-lock")
        self.runner.apply()
        self.cloud.fail_stop = True
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(self.cloud.service["taskDefinition"], self.cloud.original)
        self.assertTrue(result["errors"])
        self.assertTrue(self.journal.find("stop_intent"))
        self.assertFalse(self.journal.find("released"))

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

    def test_stopped_tag_loss_uses_durable_receipt_saved_before_control(self):
        """AWS tag deletion does not invalidate a previously verified owned task."""
        task = self.apply_full_arn_maintenance()
        expected = {
            "snapshotHash": self.journal.events[0]["hash"],
            "runId": "run-1",
            "taskArn": task["taskArn"],
            "taskDefinitionArn": task["taskDefinitionArn"],
            "startedBy": task["startedBy"],
        }

        def checked_aws(service, operation, **payload):
            if operation == "stop-task":
                receipts = demo.Journal(self.journal.path).find(
                    "task_ownership_verified"
                )
                self.assertEqual([e["data"] for e in receipts], [expected])
                self.assertEqual(
                    {t["key"]: t["value"] for t in task["tags"]},
                    self.runner.owner_tags(),
                )
            return self.cloud(service, operation, **payload)

        self.runner.aws = checked_aws
        self.runner.restore()
        self.assertEqual(task["tags"], [])
        self.advance()
        result = self.runner.restore()
        self.assertTrue(result["recoveryVerified"])
        self.assertTrue(result["checks"]["ownedMaintenanceStopped"])
        evidence = result["maintenanceRestoration"]
        self.assertEqual(evidence["stoppedBeforeRestore"], [])
        self.assertEqual(evidence["stopRequestedTasks"], [task["taskArn"]])
        self.assertEqual(evidence["exitEvidence"][0]["stopCode"], "UserInitiated")
        self.assertFalse(evidence["approvedRestorationCausedRecovery"])
        self.assertEqual(sum(op == "stop-task" for _, op, _ in self.cloud.calls), 1)

    def test_cli_new_process_restores_stopped_task_from_durable_journal(self):
        """An actual new Python process loads proof; no controller cache survives."""
        common = self.connect_cli()
        task = self.apply_full_arn_maintenance()
        self.runner.restore()
        self.assertEqual(task["tags"], [])
        cloud_file = Path(self.temp.name) / "cloud.json"
        cloud_file.write_text(json.dumps(self.cloud.__dict__))
        child = """
import json, runpy, sys
from datetime import timedelta
fixture = runpy.run_path(sys.argv[1])
demo = fixture["demo"]
cloud = fixture["Cloud"]()
cloud.__dict__.update(json.loads(open(sys.argv[2]).read()))
demo.Aws = lambda *args: cloud
demo.datetime = fixture["Clock"]
demo.datetime.current += timedelta(minutes=4)
code = demo.main(["restore", *sys.argv[3:]])
open(sys.argv[2], "w").write(json.dumps(cloud.__dict__))
sys.exit(code)
"""
        result = subprocess.run(
            [sys.executable, "-c", child, __file__, str(cloud_file), *common],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["recoveryVerified"])
        self.assertEqual(output["maintenanceTasks"][0]["taskArn"], task["taskArn"])
        self.assertEqual(output["maintenanceTasks"][0]["tags"], [])
        self.assertTrue(demo.Journal(self.journal.path).find("released"))
        calls = json.loads(cloud_file.read_text())["calls"]
        self.assertEqual(sum(op == "stop-task" for _, op, _ in calls), 1)

    def test_receipt_alone_rediscovers_expired_task_with_omitted_tags(self):
        """The receipt retains a full ARN even without launch/observation responses."""
        task = self.apply_full_arn_maintenance()
        task["lastStatus"] = "STOPPED"
        del task["tags"]
        runner = self.replay_journal(
            lambda event: (
                None
                if event["kind"] in ("maintenance_tasks", "owned_tasks", "status")
                else event
            )
        )
        self.assertEqual(runner.discovered_task_arns, set())
        runner.restore()
        self.advance()
        result = runner.restore()
        self.assertTrue(result["recoveryVerified"])
        self.assertEqual(
            result["maintenanceRestoration"]["stoppedBeforeRestore"],
            [task["taskArn"]],
        )
        self.assertFalse(any(op == "stop-task" for _, op, _ in self.cloud.calls))

    def test_stopped_without_receipt_fails_closed_despite_historical_tags(self):
        """A launch response, old observation or stop intent is not a verified receipt."""
        task = self.apply_full_arn_maintenance()
        self.journal.append("stop_intent", {"taskArn": task["taskArn"]})
        task.update(lastStatus="STOPPED", tags=[])
        runner = self.replay_journal(
            lambda event: None if event["kind"] == "task_ownership_verified" else event
        )
        result = runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertTrue(result["errors"])
        self.assertFalse(runner.journal.find("task_ownership_verified"))
        self.assertFalse(runner.journal.find("released"))
        self.assertFalse(any(op == "stop-task" for _, op, _ in self.cloud.calls))

    def test_stopped_receipt_requires_every_binding_to_match(self):
        """Proof from another snapshot/run/task/definition/start marker never transfers."""
        for field in (
            "snapshotHash",
            "runId",
            "taskArn",
            "taskDefinitionArn",
            "startedBy",
        ):
            with self.subTest(field=field):
                self.setUp()
                task = self.apply_full_arn_maintenance()
                task.update(lastStatus="STOPPED", tags=[])

                def change_receipt(event, field=field, task=task):
                    if event["kind"] == "task_ownership_verified":
                        event["data"][field] += "-foreign"
                        if field == "taskArn":
                            # Same task ID suffix, different AWS account.
                            event["data"][field] = task["taskArn"].replace(
                                ":123456789012:", ":999999999999:"
                            )
                            self.cloud.jobs[event["data"][field]] = dict(
                                task, taskArn=event["data"][field]
                            )
                    return event

                runner = self.replay_journal(change_receipt)
                result = runner.restore()
                self.assertFalse(result["recoveryVerified"])
                self.assertTrue(result["errors"])
                self.assertFalse(runner.journal.find("released"))
                self.assertFalse(
                    any(op == "stop-task" for _, op, _ in self.cloud.calls)
                )

    def test_receipt_never_authorizes_missing_live_tags(self):
        """Only the terminal STOPPED state admits tag loss, including during shutdown."""
        for state in ("RUNNING", "PENDING", "STOPPING", "DEACTIVATING", "UNKNOWN"):
            with self.subTest(state=state):
                self.setUp()
                task = self.apply_full_arn_maintenance()
                task.update(lastStatus=state, tags=[])
                result = self.runner.restore()
                self.assertFalse(result["recoveryVerified"])
                self.assertTrue(result["errors"])
                self.assertFalse(
                    any(op == "stop-task" for _, op, _ in self.cloud.calls)
                )

    def test_receipt_rejects_changed_task_identity_and_conflicting_tags(self):
        """Even a known ARN cannot mask changed identity or partial/conflicting tags."""
        for state in ("RUNNING", "STOPPED"):
            for change in (
                "startedBy",
                "definition",
                "owner",
                "proof",
                "partial",
                "duplicate",
            ):
                with self.subTest(state=state, change=change):
                    self.setUp()
                    task = self.apply_full_arn_maintenance()
                    task["lastStatus"] = state
                    if change == "startedBy":
                        task["startedBy"] = "foreign"
                    elif change == "definition":
                        task["taskDefinitionArn"] += "-other-owned-revision"
                        self.journal.append(
                            "maintenance_revision", {"arn": task["taskDefinitionArn"]}
                        )
                    elif change == "partial":
                        task["tags"] = task["tags"][:1]
                    elif change == "duplicate":
                        task["tags"].insert(0, {"key": demo.OWNER, "value": "foreign"})
                    else:
                        key = demo.OWNER if change == "owner" else demo.PROOF
                        next(t for t in task["tags"] if t["key"] == key)["value"] = (
                            "foreign"
                        )
                    result = self.runner.restore()
                    self.assertFalse(result["recoveryVerified"])
                    self.assertTrue(result["errors"])
                    self.assertFalse(
                        any(op == "stop-task" for _, op, _ in self.cloud.calls)
                    )

    def test_tags_are_rechecked_immediately_before_stop(self):
        """A tag removed after discovery still prevents StopTask."""
        task = self.apply_full_arn_maintenance()
        append = self.journal.append

        def change_after_discovery(kind, data):
            event = append(kind, data)
            if kind == "maintenance_before_restore":
                task["tags"] = []
            return event

        with patch.object(self.journal, "append", change_after_discovery):
            result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(task["lastStatus"], "RUNNING")
        self.assertFalse(any(op == "stop-task" for _, op, _ in self.cloud.calls))

    def test_expiry_between_discovery_and_stop_never_records_a_stop_request(self):
        """A naturally ended task at the final check needs no control operation."""
        task = self.apply_full_arn_maintenance()
        append = self.journal.append

        def expire_after_discovery(kind, data):
            event = append(kind, data)
            if kind == "maintenance_before_restore":
                task.update(
                    lastStatus="STOPPED",
                    tags=[],
                    stopCode="EssentialContainerExited",
                )
            return event

        with patch.object(self.journal, "append", expire_after_discovery):
            self.runner.restore()
        self.advance()
        result = self.runner.restore()
        self.assertTrue(result["recoveryVerified"])
        evidence = result["maintenanceRestoration"]
        self.assertEqual(evidence["stopRequestedTasks"], [])
        self.assertEqual(
            evidence["exitEvidence"][0]["stopCode"], "EssentialContainerExited"
        )
        self.assertFalse(evidence["approvedRestorationCausedRecovery"])
        self.assertFalse(any(op == "stop-task" for _, op, _ in self.cloud.calls))

    def test_retained_task_description_cannot_disappear_or_substitute_an_arn(self):
        """Missing descriptions and same-ID ARNs from another cluster fail closed."""
        for replacement in (None, "other-cluster"):
            with self.subTest(replacement=replacement):
                self.setUp()
                task = self.apply_full_arn_maintenance()
                task.update(lastStatus="STOPPED", tags=[])

                def changed_description(
                    service, operation, task=task, replacement=replacement, **payload
                ):
                    if operation == "describe-tasks" and payload["tasks"] == [
                        task["taskArn"]
                    ]:
                        tasks = (
                            []
                            if replacement is None
                            else [
                                dict(
                                    task,
                                    taskArn=task["taskArn"].replace(
                                        "demo-cluster/", "other-cluster/"
                                    ),
                                )
                            ]
                        )
                        return {"tasks": tasks}
                    return self.cloud(service, operation, **payload)

                self.runner.aws = changed_description
                result = self.runner.restore()
                self.assertFalse(result["recoveryVerified"])
                self.assertTrue(result["errors"])
                self.assertFalse(self.journal.find("released"))
                self.assertFalse(
                    any(op == "stop-task" for _, op, _ in self.cloud.calls)
                )

    def test_receipt_write_loss_cleans_live_task_but_cannot_verify_tagless_exit(self):
        """Best-effort cleanup survives disk/stderr failure without inventing proof."""
        self.plan("maintenance-lock")
        with (
            self.failing_journal("task_ownership_verified", persistent=True),
            patch.object(
                demo, "print", create=True, side_effect=OSError("stderr full")
            ),
            self.assertRaisesRegex(OSError, "storage failure"),
        ):
            self.runner.apply()
        task = self.cloud.jobs["arn:task/owned"]
        self.assertEqual(task["lastStatus"], "STOPPED")
        self.assertEqual(task["tags"], [])
        self.assertFalse(self.runner.cleanup_result["recoveryVerified"])
        journal = demo.Journal(self.journal.path)
        self.assertFalse(journal.find("task_ownership_verified"))
        resumed = demo.Demo(self.cloud, journal, self.cloud.target)
        self.advance()
        result = resumed.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertTrue(result["errors"])
        self.assertFalse(journal.find("released"))

    def test_expired_hold_is_recovery_state_not_approved_restore_causality(self):
        """Keep natural exit evidence and never attribute it to an approved stop."""
        self.plan("maintenance-lock")
        self.runner.apply()
        task = self.cloud.jobs["arn:task/owned"]
        task.update(
            lastStatus="STOPPED",
            tags=[],
            stopCode="EssentialContainerExited",
            stoppedReason="Essential container in task exited",
            stoppedAt=Clock.current.isoformat(),
            containers=[{"name": "healthcare", "exitCode": 0}],
        )
        self.runner.restore()
        self.advance()
        result = self.runner.restore()
        self.assertTrue(result["recoveryVerified"])
        evidence = result["maintenanceRestoration"]
        self.assertEqual(evidence["stoppedBeforeRestore"], ["arn:task/owned"])
        self.assertFalse(evidence["approvedRestorationCausedRecovery"])
        self.assertEqual(evidence["stopRequestedTasks"], [])
        self.assertEqual(
            evidence["exitEvidence"][0]["stopCode"], "EssentialContainerExited"
        )
        self.assertEqual(evidence["exitEvidence"][0]["containers"][0]["exitCode"], 0)
        self.assertFalse(any(op == "stop-task" for _, op, _ in self.cloud.calls))

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
        """A source-looking tag or r1 environment label cannot make restore immutable."""
        original = self.cloud.definitions[self.cloud.original]["taskDefinition"]
        original["containerDefinitions"][0]["environment"].append(
            {"name": "DEPLOYED_REVISION", "value": "r1"}
        )
        for image in (
            "registry.example/healthcare:latest",
            "registry.example/healthcare:r1",
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

    def test_baseline_requires_verified_r1_source_manifest_from_its_task_stream(self):
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
                self.assertRaisesRegex(RuntimeError, "source_manifest must verify r1"),
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
        self.assertEqual(json.loads(evidence["events"][0]["message"])["revision"], "r1")
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

    def test_older_mutable_snapshot_cannot_apply_or_launch_unsafe_restore(self):
        """Resuming a pre-fix journal never deploys an original whose tag can move."""
        self.plan()
        snapshot = self.journal.snapshot
        tag = "registry.example/healthcare:old-r1"
        snapshot["image"] = tag
        snapshot["taskDefinition"]["taskDefinition"]["containerDefinitions"][0][
            "image"
        ] = tag
        snapshot["tasks"][0]["containers"][0]["image"] = tag
        self.journal = demo.Journal(Path(self.temp.name) / "legacy")
        self.journal.append("snapshot", snapshot)
        self.runner = demo.Demo(self.cloud, self.journal, self.cloud.target)
        with self.assertRaisesRegex(
            RuntimeError, "original application image must use"
        ):
            self.runner.apply()
        fault = "arn:task-definition/healthcare:2"
        self.cloud.definitions[fault] = copy.deepcopy(
            self.cloud.definitions[self.cloud.original]
        )
        self.cloud.rollout(fault)
        self.cloud.definitions[self.cloud.original]["taskDefinition"][
            "containerDefinitions"
        ][0]["image"] = tag
        self.journal.append("service_revision", {"arn": fault})
        self.cloud.service["tags"].extend(
            {"key": key, "value": value}
            for key, value in self.runner.owner_tags().items()
        )
        result = self.runner.restore()
        self.assertFalse(result["recoveryVerified"])
        self.assertEqual(result["restorationState"], "pending")
        self.assertIn(
            "original application image must use", result["errors"][0]["error"]
        )
        self.assertFalse(
            any(
                operation
                in {"tag-resource", "register-task-definition", "update-service"}
                for _, operation, _ in self.cloud.calls
            )
        )

    def test_older_snapshot_without_r1_evidence_cannot_apply(self):
        """An earlier plan cannot bypass the new source check merely by using a digest."""
        self.plan()
        snapshot = self.journal.snapshot
        snapshot.pop("sourceManifests")
        journal = demo.Journal(Path(self.temp.name) / "without-source")
        journal.append("snapshot", snapshot)
        runner = demo.Demo(self.cloud, journal, self.cloud.target)
        with self.assertRaisesRegex(
            RuntimeError, "lacks verified r1 baseline source evidence"
        ):
            runner.apply()
        self.assertFalse(
            any(
                operation
                in {"tag-resource", "register-task-definition", "update-service"}
                for _, operation, _ in self.cloud.calls
            )
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

    def test_no_discovered_task_after_lost_launch_stays_unresolved(self):
        """An ambiguous launch never starts another task during restore."""
        self.plan("maintenance-lock")
        self.journal.append("run_task_intent", {})
        result = self.runner.restore()
        self.advance()
        self.assertFalse(self.runner.restore()["recoveryVerified"])
        self.assertFalse(result["checks"]["ownedMaintenanceStopped"])
        self.assertFalse(any(op == "run-task" for _, op, _ in self.cloud.calls))

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

    def test_failed_apply_uses_wait_budget_for_async_cleanup(self):
        """A failed apply still waits for verified cleanup before propagating failure."""
        self.plan()
        self.cloud.async_restore = True
        self.cloud.fail_update = True
        clock = self.polling_clock(self.complete_restore_later)
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.runner.apply(wait_seconds=300)
        cleanup = self.journal.find("restore_wait_result")[-1]["data"]
        self.assertEqual(cleanup["waitSeconds"], 300)
        self.assertTrue(cleanup["recoveryVerified"])
        self.assertGreaterEqual(clock.elapsed, 180)
        self.assertTrue(self.journal.find("released"))
        self.assertFalse(
            cleanup["maintenanceRestoration"]["approvedRestorationCausedRecovery"]
        )

    def test_failed_apply_cleanup_timeout_keeps_pending_owner(self):
        """A cleanup budget too short for fresh periods cannot be reported as success."""
        self.plan()
        self.cloud.fail_update = True
        clock = self.polling_clock()
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.runner.apply(wait_seconds=60)
        cleanup = self.journal.find("restore_wait_result")[-1]["data"]
        self.assertFalse(cleanup["recoveryVerified"])
        self.assertEqual(cleanup["restorationState"], "pending")
        self.assertTrue(cleanup["waitExpired"])
        self.assertEqual(clock.elapsed, 60)
        self.assertFalse(self.journal.find("released"))
        self.assertTrue(
            any(tag["key"] == demo.OWNER for tag in self.cloud.service["tags"])
        )

    def test_apply_timeout_gives_cleanup_a_fresh_wait_budget(self):
        """An exhausted apply wait must not consume the recovery observation budget."""
        self.plan()
        self.cloud.async_fault = True
        clock = self.polling_clock()
        with self.assertRaisesRegex(TimeoutError, "observation timed out"):
            self.runner.apply(wait_seconds=300)
        cleanup = self.journal.find("restore_wait_result")[-1]["data"]
        self.assertEqual(cleanup["waitSeconds"], 300)
        self.assertTrue(cleanup["recoveryVerified"])
        self.assertGreater(clock.elapsed, 300)
        self.assertLess(clock.elapsed, 600)

    def test_cli_pending_restore_returns_nonzero_and_can_resume(self):
        """CLI --wait-seconds is routed through the shared bounded restore path."""
        common = self.connect_cli()
        self.plan()
        self.runner.apply()
        clock = self.polling_clock()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = demo.main(["restore", *common, "--wait-seconds", "60"])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(clock.elapsed, 60)
        self.assertEqual(result["restorationState"], "pending")
        self.assertFalse(result["recoveryVerified"])
        self.assertFalse(demo.Journal(self.journal.path).find("released"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = demo.main(["restore", *common, "--wait-seconds", "300"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["recoveryVerified"])

    def test_cli_apply_failure_stays_failed_even_when_cleanup_verifies(self):
        """Operator cleanup success is separate from the failed apply operation."""
        common = self.connect_cli()
        self.plan()
        self.cloud.fail_update = True
        self.polling_clock()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = demo.main(["apply", *common, "--wait-seconds", "300"])
        result = json.loads(stderr.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(result["operationSucceeded"])
        self.assertTrue(result["cleanup"]["recoveryVerified"])
        self.assertFalse(
            result["cleanup"]["maintenanceRestoration"][
                "approvedRestorationCausedRecovery"
            ]
        )

    def test_maintenance_cli_bound_matches_app_and_passes_explicit_schema(self):
        """Keep the standalone job command aligned with the app's finite hold bound."""
        source = (
            SCRIPT.parents[1]
            / "packages/healthcare-sensor-app/src/test_service/maintenance.py"
        )
        module = ast.parse(source.read_text())
        bound = next(
            ast.literal_eval(node.value)
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "MAX_HOLD_SECONDS"
                for target in node.targets
            )
        )
        arguments = [
            "plan",
            "--run-id",
            "run-1",
            "--cluster",
            "cluster",
            "--service",
            "service",
            "--region",
            "us-east-1",
            "--journal-root",
            self.temp.name,
            "--scenario",
            "maintenance-lock",
            "--alarm",
            "ingest",
            "--alarm",
            "query",
            "--alarm",
            "connections",
        ]
        options = demo.parse_args(arguments)
        self.assertEqual(options.hold_seconds, bound)
        fractional = demo.parse_args([*arguments, "--hold-seconds", str(bound - 0.5)])
        self.assertEqual(fractional.hold_seconds, bound - 0.5)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as rejected,
        ):
            demo.parse_args([*arguments, "--hold-seconds", str(bound + 0.5)])
        self.assertEqual(rejected.exception.code, 2)
        self.plan(
            "maintenance-lock",
            hold_seconds=options.hold_seconds,
            maintenance_command=list(demo.MAINTENANCE_COMMAND),
            maintenance_schema="demo_schema",
        )
        self.runner.apply()
        request = next(
            payload
            for _, operation, payload in self.cloud.calls
            if operation == "run-task"
        )
        task = self.cloud.definitions[request["taskDefinition"]]["taskDefinition"]
        app = task["containerDefinitions"][0]
        self.assertEqual(app["entryPoint"], ["python"])
        self.assertEqual(app["command"][:2], ["-m", "test_service.maintenance"])
        self.assertEqual(
            float(app["command"][app["command"].index("--hold-seconds") + 1]), bound
        )
        self.assertEqual(
            app["command"][app["command"].index("--schema") + 1], "demo_schema"
        )
        self.assertEqual(demo.env_map(task, "healthcare")["TRAFFIC_ENABLED"], "false")

    def test_all_scenarios_preserve_measured_workload_and_observation_profile(self):
        """Faults keep every baseline workload/observer value and unrelated absence."""
        profile = {
            "TRAFFIC_ENABLED": "true",
            "TRAFFIC_INTERVAL_SECONDS": "0.5",
            "TRAFFIC_MAX_CONCURRENCY": "8",
            "TRAFFIC_QUERY_LIMIT": "40",
            "TRAFFIC_PATIENT_ID": "patient-baseline",
            "TRAFFIC_SEED": "123",
            "DB_STATEMENT_TIMEOUT_MS": "2000",
            "DB_OBSERVABILITY_ENABLED": "true",
            "DB_OBSERVABILITY_INTERVAL_SECONDS": "5",
        }
        for scenario in (
            "pool-config",
            "query-revision",
            "session-revision",
            "maintenance-lock",
        ):
            with self.subTest(scenario=scenario):
                self.setUp()
                original = self.cloud.definitions[self.cloud.original]["taskDefinition"]
                environment = demo.env_map(original, "healthcare") | profile
                original["containerDefinitions"][0]["environment"] = [
                    {"name": key, "value": value} for key, value in environment.items()
                ]
                frozen = copy.deepcopy(original)
                image_options = (
                    {
                        "image": "registry.example/healthcare@sha256:" + "b" * 64,
                        "revision": "source-id",
                    }
                    if scenario.endswith("-revision")
                    else {}
                )
                self.plan(scenario, expect_settings=profile, **image_options)
                self.runner.apply()
                active = self.cloud.definitions[self.cloud.service["taskDefinition"]][
                    "taskDefinition"
                ]
                actual = demo.env_map(active, "healthcare")
                self.assertEqual({key: actual[key] for key in profile}, profile)
                expected = environment.copy()
                if scenario != "maintenance-lock":
                    expected["RCA_TEST_RUN_ID"] = "run-1"
                if scenario == "pool-config":
                    expected.update(
                        DB_POOL_SIZE="1",
                        DB_MAX_OVERFLOW="0",
                        DB_POOL_TIMEOUT_SECONDS="0.25",
                    )
                elif scenario.endswith("-revision"):
                    expected["DEPLOYED_REVISION"] = "source-id"
                self.assertEqual(actual, expected)
                self.assertNotIn("LOG_LEVEL", actual)
                self.runner.restore()
                self.assertEqual(
                    self.cloud.service["taskDefinition"], self.cloud.original
                )
                self.assertEqual(
                    self.cloud.definitions[self.cloud.original]["taskDefinition"],
                    frozen,
                )

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

    def test_automatic_rollback_is_original_not_foreign_and_restore_is_idempotent(self):
        """Retain ECS rollback evidence and avoid another deployment when original."""
        self.plan()
        self.runner.apply()
        self.cloud.rollout(self.cloud.original)
        self.cloud.service["events"] = [
            {
                "id": "rollback-1",
                "createdAt": Clock.current.isoformat(),
                "message": "service deployment failed; rolling back to the last completed deployment",
            }
        ]
        status = self.runner.status()
        observation = status["revisionObservation"]
        self.assertEqual(observation["state"], "original-after-apply")
        self.assertEqual(observation["returnCause"], "ecs-rollback-event")
        self.assertTrue(observation["faultRevisionPreviouslyObserved"])
        self.assertIsNone(status["ownershipError"])
        with self.assertRaisesRegex(RuntimeError, "returned to original"):
            self.runner.wait_applied(1)
        count = len(self.cloud.calls)
        result = self.runner.restore()
        self.assertTrue(result["revisionObservation"]["alreadyOriginalAtRestore"])
        self.advance()
        self.assertTrue(self.runner.restore()["recoveryVerified"])
        self.assertFalse(
            any(op == "update-service" for _, op, _ in self.cloud.calls[count:])
        )

    def test_return_to_original_without_rollback_evidence_keeps_cause_unknown(self):
        """An already-restored original revision alone does not prove automatic rollback."""
        self.plan()
        self.runner.apply()
        self.cloud.rollout(self.cloud.original)
        observation = self.runner.status()["revisionObservation"]
        self.assertEqual(observation["state"], "original-after-apply")
        self.assertEqual(observation["returnCause"], "not-established")
        self.assertEqual(observation["rollbackEventEvidence"], [])

    def test_foreign_revision_is_not_an_already_restored_service(self):
        """A different ARN must never be overwritten even when its settings look healthy."""
        self.plan()
        self.runner.apply()
        foreign = "arn:task-definition/healthcare:999"
        self.cloud.definitions[foreign] = copy.deepcopy(
            self.cloud.definitions[self.cloud.original]
        )
        self.cloud.rollout(foreign)
        status = self.runner.status()
        self.assertEqual(status["revisionObservation"]["state"], "foreign-revision")
        self.assertIn("foreign deployment", status["ownershipError"])
        count = len(self.cloud.calls)
        self.assertFalse(self.runner.restore()["recoveryVerified"])
        self.assertFalse(
            any(op == "update-service" for _, op, _ in self.cloud.calls[count:])
        )

    def test_cli_service_lock_and_prior_run_refuse_competing_plan(self):
        """Canonical service locks and unrestored journals prevent parallel writers."""
        previous_aws = demo.Aws
        demo.Aws = lambda *args: self.cloud
        self.addCleanup(setattr, demo, "Aws", previous_aws)
        base = [
            "--cluster",
            "cluster-name",
            "--service",
            "service-name",
            "--region",
            "us-east-1",
            "--journal-root",
            self.temp.name,
            "--scenario",
            "pool-config",
            "--alarm",
            "ingest",
            "--alarm",
            "query",
            "--alarm",
            "connections",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(demo.main(["plan", "--run-id", "cli-run", *base]), 0)
        with self.assertRaisesRegex(RuntimeError, "unrestored local RunId"):
            demo.main(["plan", "--run-id", "other-run", *base])
        directory = Path(self.temp.name) / demo.digest(self.cloud.target)
        with (directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "owns this service lock"):
                demo.main(["plan", "--run-id", "parallel-run", *base])


if __name__ == "__main__":
    unittest.main()
