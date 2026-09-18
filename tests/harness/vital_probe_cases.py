"""Exercise the default ECS probe contract without credentials or AWS traffic."""

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import realistic_demo_cases as existing

demo = existing.demo


class ProbeTests(unittest.TestCase):
    def setUp(self):
        """Use the existing local service model and a source-bound normal probe manifest."""
        existing.DemoTests.setUp(self)
        app = self.cloud.definitions[self.cloud.original]["taskDefinition"][
            "containerDefinitions"
        ][0]
        app["environment"].append({"name": "DB_NAME", "value": "healthcare"})
        source = self.cloud.source_manifest
        source["files"]["backlog_probe.py"] = hashlib.sha256(
            (
                Path(__file__).resolve().parents[2]
                / "packages/healthcare-sensor-app/src/test_service/backlog_probe.py"
            ).read_bytes()
        ).hexdigest()
        source["fingerprint"] = hashlib.sha256(
            json.dumps(source["files"], sort_keys=True).encode()
        ).hexdigest()
        existing.DemoTests.plan(self)
        self.mode = "ok"
        self.task = None
        self.launches = []
        self.stops = []
        self.cancelled_once = False
        self.list_queries = []
        original = self.runner.aws

        def aws(service, operation, /, _deadline=None, **payload):
            """Model one idempotent task, explicit stop and its own CloudWatch result."""
            if operation == "run-task":
                self.launches.append(copy.deepcopy(payload))
                command = payload["overrides"]["containerOverrides"][0]["command"]
                self.assertEqual(
                    command[:3], ["python", "-m", "test_service.backlog_probe"]
                )
                self.assertEqual(payload["taskDefinition"], self.cloud.original)
                self.assertEqual(
                    payload["networkConfiguration"],
                    self.journal.snapshot["service"]["networkConfiguration"],
                )
                self.assertFalse(payload["enableExecuteCommand"])
                if self.task is None:
                    self.task = {
                        "taskArn": "arn:task/probe",
                        "clusterArn": self.cloud.target["cluster"],
                        "taskDefinitionArn": self.cloud.original,
                        "startedBy": payload["startedBy"],
                        "lastStatus": "RUNNING"
                        if self.mode in {"timeout", "cancel"}
                        else "STOPPED",
                        "desiredStatus": "RUNNING",
                        "containers": [
                            {
                                "name": "healthcare",
                                "exitCode": 0,
                                "imageDigest": self.cloud.healthy_digest,
                            }
                        ],
                    }
                if (
                    self.mode == "lost_both"
                    or self.mode == "lost_reply"
                    and len(self.launches) == 1
                ):
                    raise RuntimeError("response lost")
                return {"tasks": [copy.deepcopy(self.task)]}
            if operation == "list-tasks":
                self.list_queries.append(copy.deepcopy(payload))
                if "startedBy" in payload:
                    self.assertEqual(set(payload), {"cluster", "startedBy"})
                    return {
                        "taskArns": [self.task["taskArn"]]
                        if self.task and self.task["lastStatus"] != "STOPPED"
                        else []
                    }
                if payload.get("desiredStatus") == "STOPPED":
                    self.assertNotIn("startedBy", payload)
                    return {
                        "taskArns": ["arn:task/other", "arn:task/probe"]
                        if self.task
                        else ["arn:task/other"]
                    }
            if operation == "describe-tasks" and set(payload["tasks"]) <= {
                "arn:task/probe",
                "arn:task/other",
            }:
                if self.mode == "cancel" and not self.cancelled_once:
                    self.cancelled_once = True
                    raise KeyboardInterrupt("local cancellation")
                result = copy.deepcopy(self.task)
                if self.mode == "bad_image":
                    result["containers"][0]["imageDigest"] = "sha256:" + "f" * 64
                if self.mode == "bad_exit":
                    result["containers"][0]["exitCode"] = 1
                tasks = [result] if "arn:task/probe" in payload["tasks"] else []
                if "arn:task/other" in payload["tasks"]:
                    tasks.append(
                        {
                            **result,
                            "taskArn": "arn:task/other",
                            "startedBy": "someone-else",
                        }
                    )
                return {"tasks": tasks}
            if operation == "stop-task":
                self.assertEqual(payload["task"], "arn:task/probe")
                self.stops.append(payload["task"])
                self.task.update(lastStatus="STOPPED", desiredStatus="STOPPED")
                return {"task": copy.deepcopy(self.task)}
            if operation == "get-log-events":
                self.assertTrue(payload["logStreamName"].endswith("/healthcare/probe"))
                request = self.launches[-1]
                value = {
                    "event": "vital_backlog_proof",
                    "probe_id": request["startedBy"],
                    "run_id": "run-1",
                    "operation": "checkpoint",
                    "source_fingerprint": source["fingerprint"],
                    "probe_source_sha256": source["files"]["backlog_probe.py"],
                    "observed_at": existing.Clock.current.isoformat(),
                    "database": "healthcare",
                    "schema": "public",
                    "result": {
                        "epoch": "11111111-2222-4333-8444-555555555555",
                        "lower_exclusive": 0,
                        "upper_inclusive": 0,
                        "observed_at": existing.Clock.current.isoformat(),
                    },
                }
                if self.mode == "foreign_output":
                    value["run_id"] = "other"
                if self.mode == "private_extra":
                    value["result"]["private_payload"] = "PRIVATE_SENTINEL"
                return {"events": [{"message": json.dumps(value)}]}
            return original(service, operation, **payload)

        self.runner.aws = aws

    def test_default_requires_no_env_file_and_binds_pinned_task_image_and_output(self):
        """The laptop path launches a fixed task without new endpoint, credentials or role overrides."""
        result = self.runner.backlog("backlog-open", wait_seconds=2)
        self.assertEqual(result["source"]["transport"], "ecs_standalone_readonly")
        self.assertEqual(result["source"]["exit_code"], 0)
        self.assertEqual(len(self.launches), 1)
        self.assertNotIn("taskRoleArn", self.launches[0]["overrides"])
        self.assertTrue(self.journal.find("backlog_probe_stopped"))
        args = demo.parse_args(
            [
                "backlog-open",
                "--run-id",
                "run",
                "--cluster",
                "c",
                "--service",
                "s",
                "--region",
                "us-east-1",
                "--journal-root",
                "/unused",
            ]
        )
        self.assertIsNone(args.vital_db_env_file)

    def test_lost_launch_reply_reuses_identical_client_token(self):
        """Transport retry is the same logical task, not a second invocation."""
        self.mode = "lost_reply"
        self.runner.backlog("backlog-open", wait_seconds=2)
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(self.launches[0], self.launches[1])

    def test_two_lost_run_replies_find_stopped_task_without_combining_started_by_and_status(
        self,
    ):
        """A fast completed probe is recovered by a separate stopped-family read, ignoring unrelated tasks."""
        self.mode = "lost_both"
        result = self.runner.backlog("backlog-open", wait_seconds=2)
        self.assertEqual(result["source"]["task_arn"], "arn:task/probe")
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(self.launches[0], self.launches[1])
        self.assertTrue(
            any(
                q.get("desiredStatus") == "STOPPED" and "family" in q
                for q in self.list_queries
            )
        )
        self.assertEqual(self.stops, [])

    def test_wrong_image_exit_or_unbound_summary_never_publishes_checkpoint(self):
        """A STOPPED task alone is insufficient authority for a database summary."""
        for mode in ("bad_image", "bad_exit", "foreign_output", "private_extra"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.task = None
                with self.assertRaises((RuntimeError, ValueError)):
                    self.runner.backlog("backlog-open", wait_seconds=2)
                self.assertFalse(self.journal.find("backlog_open"))
                self.assertNotIn("PRIVATE_SENTINEL", str(self.journal.events))

    def test_timeout_stops_and_drains_only_owned_probe(self):
        """A bounded task wait may fail, but cannot leave the owned task running or accept its output."""
        self.mode = "timeout"
        clock = iter(i * 0.05 for i in range(10000))
        with (
            patch("time.monotonic", side_effect=lambda: next(clock)),
            patch("time.sleep"),
            self.assertRaisesRegex(RuntimeError, "wait window"),
        ):
            self.runner.backlog("backlog-open", wait_seconds=2)
        self.assertEqual(self.stops, ["arn:task/probe"])
        self.assertEqual(self.task["lastStatus"], "STOPPED")
        self.assertFalse(self.journal.find("backlog_open"))

    def test_cancellation_drains_owned_task_before_returning(self):
        """Cancellation is preserved after the recorded owned task reaches STOPPED."""
        self.mode = "cancel"
        with patch("time.sleep"), self.assertRaises(KeyboardInterrupt):
            self.runner.backlog("backlog-open", wait_seconds=2)
        self.assertEqual(self.stops, ["arn:task/probe"])
        self.assertTrue(self.journal.find("backlog_probe_stopped"))
        self.assertEqual(len(self.launches), 1)

    def test_log_pagination_cannot_reset_the_query_deadline(self):
        """An endless log cursor ends inside the original wait window, never publishing partial output."""
        original = self.runner.aws
        pages = []

        def paginated(service, operation, /, **payload):
            """Provide unique endless cursors only in the local log transport."""
            if operation == "get-log-events":
                pages.append(payload.get("nextToken"))
                return {"events": [], "nextForwardToken": str(len(pages))}
            return original(service, operation, **payload)

        self.runner.aws = paginated
        clock = iter(i * 0.05 for i in range(10000))
        with (
            patch("time.monotonic", side_effect=lambda: next(clock)),
            patch("time.sleep"),
            self.assertRaisesRegex(RuntimeError, "wait window"),
        ):
            self.runner.backlog("backlog-open", wait_seconds=2)
        self.assertGreater(len(pages), 0)
        self.assertLess(len(pages), 40)
        self.assertFalse(self.journal.find("backlog_open"))

    def test_discovery_pagination_keeps_cleanup_unknown_without_extending_total_budget(
        self,
    ):
        """No match on endlessly paginated lists cannot become a proven empty task set."""
        self.mode = "lost_both"
        original = self.runner.aws
        pages = []

        def paginated(service, operation, /, **payload):
            """Hide every task behind an endless cursor without exposing unrelated task IDs."""
            if operation == "list-tasks":
                pages.append(payload.get("nextToken"))
                return {"taskArns": [], "nextToken": str(len(pages))}
            return original(service, operation, **payload)

        self.runner.aws = paginated
        clock = iter(i * 0.05 for i in range(10000))
        with (
            patch("time.monotonic", side_effect=lambda: next(clock)),
            patch("time.sleep"),
            self.assertRaisesRegex(RuntimeError, "UNKNOWN"),
        ):
            self.runner.backlog("backlog-open", wait_seconds=2)
        self.assertLess(len(pages), 45)
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(self.stops, [])
        self.assertTrue(self.journal.find("backlog_probe_cleanup_unconfirmed"))
        self.assertFalse(self.journal.find("backlog_open"))
        with self.assertRaisesRegex(RuntimeError, "previous probe"):
            self.runner.backlog("backlog-open", wait_seconds=2)
        self.assertEqual(len(self.launches), 2)


if __name__ == "__main__":
    unittest.main()
