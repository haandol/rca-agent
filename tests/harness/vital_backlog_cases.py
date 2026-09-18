"""Root backlog boundaries and sanitizers; all AWS interactions are explicit local doubles."""

import copy
import json
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import realistic_demo_cases as existing

demo = existing.demo
EPOCH = "11111111-2222-4333-8444-555555555555"
READING = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class BacklogTests(unittest.TestCase):
    def setUp(self):
        """Reuse the existing local cloud model without starting any AWS client."""
        existing.DemoTests.setUp(self)
        self.plan = lambda: existing.DemoTests.plan(self)
        self.requests = []
        self.pending = True
        self.provide_witness = True
        self.wrong_task = False
        self.summary_change = {}
        self.epoch = EPOCH
        self.contract = {
            "format": "vital-event-v1-v2",
            "delivery_stage": "measurement_attempt",
        }
        source = self.runner.aws

        def normal(service, operation, /, **payload):
            """Include a correlated optional Vital receipt in the existing local normal capture."""
            response = source(service, operation, **payload)
            if (
                operation == "filter-log-events"
                and "input_contract_observed" in payload.get("filterPattern", "")
            ):
                write = next(
                    e
                    for e in response["events"]
                    if json.loads(e["message"])["event"] == "write_completed"
                )
                message = json.loads(write["message"])
                message.update(request_id="a" * 32, reading_ref=READING)
                write["message"] = json.dumps(message)
                receipt = copy.deepcopy(write)
                receipt["eventId"] = "normal-input"
                receipt["message"] = json.dumps(
                    {
                        "event": "input_contract_observed",
                        "observed_at": message["observed_at"],
                        "request_id": "a" * 32,
                        "operation": "ingest",
                        "count": 1,
                        "event_schema_version": 2,
                        "input_contract": self.contract,
                        "input_contract_sha256": existing.hashlib.sha256(
                            demo.canonical(self.contract)
                        ).hexdigest(),
                    }
                )
                response["events"].append(receipt)
            return response

        self.runner.aws = normal
        self.plan()

    def reader(self, snapshot, env_file, operation, **kwargs):
        """Return a labeled local transport fixture; production uses a real DB subprocess."""
        self.requests.append((operation, copy.deepcopy(kwargs)))
        if operation == "checkpoint":
            value = {
                "epoch": self.epoch,
                "lower_exclusive": 2,
                "upper_inclusive": 5 if len(self.requests) == 1 else 9,
                "observed_at": existing.Clock.current.isoformat(),
            }
        else:
            value = {
                **kwargs["cohort"],
                "expected_events": 7,
                "retained_identities": 7,
                "pending_events": 1 if self.pending else 0,
                "missing_measurements": 0,
                "payload_mismatches": 0,
                "matched_measurements": 6 if self.pending else 7,
                "lost_pending_inputs": 0,
                "missing_identities": 0,
                "observed_42703_events": 1 if self.provide_witness else 0,
                "latest_42703_at": self.fault_at,
                "complete": not self.pending,
                **self.summary_change,
            }
        return {"result": value}

    def open_and_apply(self):
        """Fix the lower boundary before the owned fault update."""
        self.runner.backlog("backlog-open", "not-read-by-fixture", reader=self.reader)
        existing.Clock.current += timedelta(seconds=1)
        self.fault_at = existing.Clock.current.isoformat()
        self.runner.apply()
        self.fault_definition = self.cloud.service["taskDefinition"]

    def close(self):
        """Simulate external normal rollback; the proof must never perform it itself."""
        self.cloud.rollout(self.cloud.original)
        existing.Clock.current += timedelta(seconds=10)
        return self.runner.backlog("backlog-close", "fixture", reader=self.reader)

    def test_fixed_range_includes_drained_and_pending_and_rechecks_without_new_bound(
        self,
    ):
        self.open_and_apply()
        with self.assertRaisesRegex(RuntimeError, "converge"):
            self.runner.backlog("backlog-close", "fixture", reader=self.reader)
        closed = self.close()
        self.assertEqual(
            closed["cohort"],
            {"epoch": EPOCH, "lower_exclusive": 2, "upper_inclusive": 9},
        )
        first = self.runner.backlog("backlog-check", "fixture", reader=self.reader)
        self.assertFalse(first["backlogVerified"])
        self.pending = False
        second = self.runner.backlog("backlog-check", "fixture", reader=self.reader)
        self.assertTrue(second["backlogVerified"])
        self.assertEqual(first["cohort_sha256"], second["cohort_sha256"])
        self.assertFalse(second["scenarioSuccess"])
        self.assertFalse(second["executionStateChanged"])
        prior = len(self.requests)
        self.assertEqual(
            self.runner.backlog("backlog-close", "fixture", reader=self.reader), closed
        )
        self.assertEqual(len(self.requests), prior)

    def test_late_open_and_epoch_change_cannot_choose_a_new_subset(self):
        self.journal.append("apply_intent", {})
        with self.assertRaisesRegex(RuntimeError, "before fault"):
            self.runner.backlog("backlog-open", "fixture", reader=self.reader)
        self.assertFalse(self.requests)

    def test_vital_apply_without_open_makes_no_cloud_calls_or_fault_intent(self):
        """Missing pre-fault coverage must fail before even the first cloud mutation."""
        before = len(self.cloud.calls)
        with self.assertRaisesRegex(RuntimeError, "backlog-open"):
            self.runner.apply()
        self.assertEqual(len(self.cloud.calls), before)
        self.assertFalse(self.journal.find("apply_intent"))

    def test_vital_apply_rejects_unbound_checkpoint_before_cloud_calls(self):
        """A structurally valid checkpoint must still equal its captured reader result."""
        self.runner.backlog("backlog-open", "fixture", reader=self.reader)
        self.journal.find("backlog_open")[0]["data"]["boundary"] = "after_fault"
        before = len(self.cloud.calls)
        with self.assertRaisesRegex(RuntimeError, "backlog-open"):
            self.runner.apply()
        self.assertEqual(len(self.cloud.calls), before)
        self.assertFalse(self.journal.find("apply_intent"))

    def test_changed_epoch_refuses_closure(self):
        self.open_and_apply()
        self.epoch = "ffffffff-2222-4333-8444-555555555555"
        with self.assertRaisesRegex(RuntimeError, "epoch"):
            self.close()
        self.assertFalse(self.journal.find("backlog_close"))

    def test_missing_foreign_or_unjoined_error_proof_never_becomes_complete(self):
        self.open_and_apply()
        self.close()
        self.pending = False
        self.provide_witness = False
        self.assertFalse(
            self.runner.backlog("backlog-check", "fixture", reader=self.reader)[
                "backlogVerified"
            ]
        )
        self.provide_witness = True
        self.summary_change = {
            "latest_42703_at": (
                existing.Clock.current - timedelta(minutes=1)
            ).isoformat()
        }
        self.assertFalse(
            self.runner.backlog("backlog-check", "fixture", reader=self.reader)[
                "backlogVerified"
            ]
        )

    def test_bad_counts_complete_flag_or_database_read_failure_fail_closed(self):
        self.open_and_apply()
        self.close()
        self.pending = False
        for mutation in [
            {"missing_identities": 1},
            {"lost_pending_inputs": 1},
            {"pending_events": 1},
            {"matched_measurements": 8},
            {"payload_mismatches": 1},
            {"expected_events": True},
            {"complete": False},
            {"epoch": "wrong"},
        ]:
            with self.subTest(mutation=mutation):
                self.summary_change = mutation
                self.assertFalse(
                    self.runner.backlog("backlog-check", "fixture", reader=self.reader)[
                        "backlogVerified"
                    ]
                )
        with patch.object(self, "reader", side_effect=RuntimeError("read failed")):
            result = self.runner.backlog("backlog-check", "fixture", reader=self.reader)
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_actual_error_count_and_time_must_belong_to_the_fixed_cohort_window(self):
        """Legacy/global or out-of-window errors cannot substitute for actual retained identity history."""
        self.open_and_apply()
        self.close()
        self.pending = False
        for mutation in (
            {"observed_42703_events": 0},
            {"observed_42703_events": True},
            {"observed_42703_events": 8},
            {"latest_42703_at": None},
            {
                "latest_42703_at": (
                    existing.Clock.current + timedelta(seconds=1)
                ).isoformat()
            },
            {"latest_42703_at": "2020-01-01T00:00:00"},
        ):
            with self.subTest(mutation=mutation):
                self.summary_change = mutation
                result = self.runner.backlog(
                    "backlog-check", "fixture", reader=self.reader
                )
                self.assertFalse(result["backlogVerified"])

    def test_transport_failure_does_not_print_credentials_or_accept_json_input(self):
        snapshot = self.journal.snapshot
        app = snapshot["taskDefinition"]["taskDefinition"]["containerDefinitions"][0]
        app["environment"] += [
            {"name": key, "value": value}
            for key, value in {
                "DB_HOST": "127.0.0.1",
                "DB_PORT": "15444",
                "DB_NAME": "rca_demo",
            }.items()
        ]
        with patch.object(demo.subprocess, "Popen") as run:
            run.return_value.returncode = 1
            run.return_value.poll.return_value = 1
            run.return_value.communicate.return_value = ("", "PRIVATE_DSN_SECRET")
            with self.assertRaisesRegex(
                RuntimeError, "actual read-only database proof failed"
            ) as caught:
                demo.read_vital_database(snapshot, Path("private.env"), "checkpoint")
        self.assertNotIn("PRIVATE_DSN_SECRET", str(caught.exception))
        self.assertNotIn(
            "PRIVATE_DSN_SECRET", run.return_value.communicate.call_args.args[0]
        )

    def test_local_timeout_and_cancellation_reap_only_the_owned_child(self):
        """A real isolated child ignoring TERM is killed and reaped before the failed proof returns."""
        snapshot = self.journal.snapshot
        app = snapshot["taskDefinition"]["taskDefinition"]["containerDefinitions"][0]
        app["environment"] += [
            {"name": k, "value": v}
            for k, v in {
                "DB_HOST": "127.0.0.1",
                "DB_PORT": "15444",
                "DB_NAME": "rca_demo",
            }.items()
        ]
        original = subprocess.Popen
        for cancelled in (False, True):
            children = []

            def launch(_argv, *, cancel=cancelled, owned_children=children, **kwargs):
                """Substitute only the test program while retaining production process-group ownership."""
                child = original(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                            "print('ready',flush=True); time.sleep(30)"
                        ),
                    ],
                    **kwargs,
                )
                self.assertEqual(child.stdout.readline().strip(), "ready")
                owned_children.append(child)
                if cancel:
                    communicate = child.communicate
                    first = True

                    def interrupt_once(*args, **options):
                        """Raise cancellation once, allowing cleanup to drain the actual child."""
                        nonlocal first
                        if first:
                            first = False
                            raise KeyboardInterrupt("test cancellation")
                        return communicate(*args, **options)

                    child.communicate = interrupt_once
                return child

            with (
                self.subTest(cancelled=cancelled),
                patch.object(demo.subprocess, "Popen", side_effect=launch),
                self.assertRaises(
                    KeyboardInterrupt if cancelled else subprocess.TimeoutExpired
                ),
            ):
                demo.read_vital_database(
                    snapshot, "not-read", "checkpoint", wait_seconds=1
                )
            self.assertIsNotNone(children[0].poll())
            with self.assertRaises(ProcessLookupError):
                os.kill(children[0].pid, 0)


if __name__ == "__main__":
    unittest.main()
