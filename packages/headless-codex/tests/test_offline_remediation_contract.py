"""Offline boundary journeys, not live-demo evidence.

Real approval loading, DynamoDB adapter (Moto), MCP handlers, command gate, metric
poller, reducer, orchestrator, retrospective merge and S3 serializer. Only the
model runner, child processes, clock, S3 transport and vector transport are local
doubles. No archived command is executed and no production credentials are used.
"""

import copy
import hashlib
import io
import json
import shlex
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws
from structlog.testing import capture_logs

from headless_codex import execution_mcp_server as commands
from headless_codex import retrospective_mcp_server as retrospective
from headless_codex.adapters.secondary.evidence import s3_evidence_store
from headless_codex.adapters.secondary.evidence.s3_evidence_store import S3EvidenceStore
from headless_codex.adapters.secondary.execution import dynamodb_execution_store
from headless_codex.adapters.secondary.execution.dynamodb_execution_store import DynamoDbExecutionStore
from headless_codex.ports.dto.models import CodexResult
from headless_codex.ports.interfaces.execution_store import ExecutionClaimLostError
from headless_codex.services import execution_workspace as workspace
from headless_codex.services.execution_outcome import assemble_evidence, judge_resolution
from headless_codex.services.execution_pipeline import ExecutionOrchestrator
from headless_codex.services.post_action_metrics import ObservationBudget, timestamp, utc

REGION = "us-east-1"
ACCOUNT = "123456789012"
TABLE = "offline-remediation"
RCA = "offline-rca"
EXECUTION = "offline-execution"
ENGINE = "headless-codex"
SNAPSHOT = f"approved/{RCA}/{EXECUTION}/playbook.json"
PREFIX = f"executions/{RCA}/{EXECUTION}/"
REVISION = f"{ENGINE}#PLAYBOOK_REVISION"
STAGE = f"{ENGINE}#PLAYBOOK_REVISION_STAGE#{EXECUTION}"
TASK = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/demo/approved-owner"
CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/demo"
STOP = timestamp("2026-09-10T12:34:07Z")
START = timestamp("2026-09-10T12:35:00Z")
FILTER = '{ ($.event = "maintenance_released") && ($.rollback_complete = true) }'
LOG_COMMAND = (
    "aws logs filter-log-events --log-group-name /offline/owner "
    f"--filter-pattern '{FILTER}' --start-time 1789043640000 --end-time 1789043940000 --region {REGION}"
)
RATIONALE = "Observed exact owner release and independent completed writes; retained evidence. " * 16
METRICS = {
    role: {"namespace": "Offline/Writes", "metric_name": name, "dimensions": {"Service": "demo"}}
    for role, name in {"attempts": "WriteCompletions", "failures": "WriteErrors"}.items()
}
ALARM = {
    "AlarmName": "offline-write-errors",
    "AlarmArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:offline-write-errors",
    "Namespace": "Offline/Writes",
    "MetricName": "WriteErrors",
    "Dimensions": [{"Name": "Service", "Value": "demo"}],
    "Period": 60,
    "Statistic": "Sum",
    "Threshold": 1,
    "ComparisonOperator": "GreaterThanOrEqualToThreshold",
    "StateValue": "OK",
}
PLAYBOOK = {
    "playbook_id": "offline-playbook",
    "verification_status": "DRAFT",
    "failure_type": "owned lock",
    "execution_steps": [
        {
            "step_id": "action",
            "commands": [
                f"aws ecs stop-task --task {TASK} --cluster demo --region {REGION}",
                LOG_COMMAND,
                f"aws cloudwatch list-metrics --namespace Offline/Writes --region {REGION}",
                f"aws cloudwatch describe-alarms --alarm-names {ALARM['AlarmName']} --region {REGION}",
            ],
            "intent": "Release the approved owner",
            "action": f"Stop only {TASK}",
            "success_criteria": "Approved owner stopped and rollback_complete=true",
        },
        {
            "step_id": "verify",
            "metric_wait": {
                "action_step_id": "action",
                "metrics": METRICS,
                "failure_alarm_name": ALARM["AlarmName"],
                "region": REGION,
                "max_wait_seconds": 12,
            },
            "intent": "Observe recovery",
            "action": "Observe completed writes and the exact failure alarm",
            "success_criteria": "WriteErrors Sum=0, completed writes and offline-write-errors OK in two full bins",
        },
    ],
}


def _forbidden(*args, **kwargs):
    raise AssertionError("offline contract attempted a real network connection or child process")


class MemoryS3:
    def __init__(self, journey):
        self.journey = journey
        self.objects = {SNAPSHOT: json.dumps(PLAYBOOK).encode()}
        self.fail_suffix = ""

    def get_object(self, **request):
        assert request["Bucket"] == "offline-evidence"
        return {"Body": io.BytesIO(self.objects[request["Key"]])}

    def put_object(self, **request):
        key = request["Key"]
        assert request["Bucket"] == "offline-evidence" and request["ContentType"] == "application/json"
        assert self.journey.work.path.is_dir(), "evidence must outlive workspace cleanup"
        if self.fail_suffix and key.endswith(self.fail_suffix):
            raise OSError("injected local persistence failure")
        self.objects[key] = request["Body"]
        self.journey.events.append(key)


class Journey:
    """A deterministic model script; all decisions under test remain production code."""

    def __init__(self, monkeypatch, ddb):
        self.patch, self.ddb = monkeypatch, ddb
        self.now = STOP
        self.mode = "confirmed"
        self.update = {}
        self.events, self.spawned, self.records = [], [], []
        self.retrospectives = 0
        self.vector_success = True
        self.work = None
        self.s3 = MemoryS3(self)
        self.store = DynamoDbExecutionStore(ddb)
        self.container = SimpleNamespace(
            execution_store=self.store,
            evidence_store=S3EvidenceStore(self.s3),
            execution_runner=self,
            playbook_store=SimpleNamespace(save_to_s3_vectors=self.publish_vectors),
        )
        self.approval = {
            "execution_id": EXECUTION,
            "rca_id": RCA,
            "engine": ENGINE,
            "approval_id": "offline-approval",
            "requested_by": "offline-operator",
            "report_s3_key": f"reports/{ENGINE}/{RCA}/report.md",
            "approved_playbook_s3_key": SNAPSHOT,
            "playbook_digest": hashlib.sha256(self.s3.objects[SNAPSHOT]).hexdigest(),
        }
        self.put(f"EXEC#{EXECUTION}", **self.approval, execution_state="PENDING_APPROVAL")
        self.put("EXEC_ACTIVE", execution_id=EXECUTION)
        self.put(
            "ANALYSIS#SESSION",
            engine=ENGINE,
            state="COMPLETED",
            alarm_name=ALARM["AlarmName"],
            report_s3_key=self.approval["report_s3_key"],
            alarm_data=json.dumps({"AWSAccountId": ACCOUNT, "AlarmArn": ALARM["AlarmArn"]}),
        )
        # An already changed current revision must never replace the approved bytes.
        self.current = copy.deepcopy(PLAYBOOK)
        self.current["execution_steps"][0].update(
            step_id="unapproved-new-step", action="Stop a different owner", success_criteria="Unapproved criterion"
        )
        self.put(REVISION, playbook=json.dumps(self.current), publication_status="PUBLISHED")
        monkeypatch.setattr(commands, "time", SimpleNamespace(time=lambda: self.now))
        monkeypatch.setattr(commands, "_now_iso", lambda: utc(self.now))
        monkeypatch.setattr(
            commands,
            "ObservationBudget",
            lambda deadline, control: ObservationBudget(
                deadline, control, clock=lambda: self.now, monotonic=lambda: self.now, wait=self.advance
            ),
        )
        monkeypatch.setattr(commands.subprocess, "run", self.run_child)
        monkeypatch.setattr(commands.subprocess, "Popen", self.open_child)

    def put(self, sk, **fields):
        self.ddb.put_item(
            TableName=TABLE,
            Item={"PK": {"S": f"RCA#{RCA}"}, "SK": {"S": sk}, **{k: {"S": v} for k, v in fields.items()}},
        )

    def get(self, sk):
        return self.ddb.get_item(
            TableName=TABLE, Key={"PK": {"S": f"RCA#{RCA}"}, "SK": {"S": sk}}, ConsistentRead=True
        ).get("Item", {})

    def heartbeat(self):
        workspace.write_observation_json(
            workspace.observation_control_path_for_token(self.work.token),
            {
                "execution_id": EXECUTION,
                "checked_at_epoch": self.now,
                "deadline_epoch": START + 1000,
                "active": True,
            },
        )

    def advance(self, seconds):
        self.now += seconds
        self.heartbeat()

    def child_result(self, argv):
        assert isinstance(argv, list) and argv[0] == "aws"
        self.spawned.append(argv)
        operation = argv[2]
        if operation == "stop-task":
            if self.mode == "failed-stop":
                return subprocess.CompletedProcess(argv, 255, "", "AccessDeniedException: stop refused")
            payload = {"task": {"taskArn": TASK, "clusterArn": CLUSTER, "lastStatus": "STOPPED"}}
        elif operation == "filter-log-events":
            assert argv[argv.index("--filter-pattern") + 1] == FILTER
            payload = {
                "events": [
                    {
                        "message": json.dumps(
                            {"event": "maintenance_released", "rollback_complete": True, "taskArn": TASK}
                        )
                    },
                    {"message": json.dumps({"operation": "ingest", "outcome": "ok", "completed_writes": 12})},
                ]
            }
        elif operation == "list-metrics":
            payload = {
                "Metrics": [
                    {"Namespace": m["namespace"], "MetricName": m["metric_name"], "Dimensions": ALARM["Dimensions"]}
                    for m in METRICS.values()
                ]
            }
        elif operation == "describe-alarms":
            payload = {"MetricAlarms": [ALARM]}
        elif operation == "get-metric-statistics":
            name = argv[argv.index("--metric-name") + 1]
            assert argv[argv.index("--start-time") + 1] == utc(START)
            assert argv[argv.index("--end-time") + 1] == utc(START + 120)
            points = [
                {"Timestamp": utc(t), "Sum": 0 if name == "WriteErrors" else 12, "SampleCount": 2, "Unit": "Count"}
                for t in (START, START + 60)
            ]
            if name == "WriteErrors" and self.mode == "unobservable":
                points = []
            if name == "WriteErrors" and self.mode == "unhealthy":
                points[0]["Sum"] = 1
            payload = {"Label": name, "Datapoints": points}
        else:
            raise AssertionError(f"unplanned offline command: {argv}")
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    def run_child(self, argv, **kwargs):
        assert not kwargs.get("shell", False)
        return self.child_result(argv)

    def open_child(self, argv, **kwargs):
        # Keep the production cancellable subprocess loop; replace only the OS child.
        assert not kwargs.get("shell", False)
        assert any(r.get("phase") == "started" for r in self.work.read_records())
        result = self.child_result(argv)
        return SimpleNamespace(returncode=result.returncode, communicate=lambda **kw: (result.stdout, result.stderr))

    def run_execution(
        self, prompt, *, execution_token, execution_id, approved_step_ids, approved_success_criteria, cancel_checker
    ):
        self.work = workspace.ExecutionWorkspace(execution_id, execution_token)
        assert not cancel_checker()
        assert approved_step_ids == ("action", "verify")
        assert "unapproved-new-step" not in prompt and TASK in prompt
        context = json.loads(workspace.observation_context_path_for_token(execution_token).read_text())
        assert context["playbook"] == PLAYBOOK
        for key, value in (
            (workspace.EXECUTION_TOKEN_ENV, execution_token),
            (workspace.EXECUTION_ID_ENV, execution_id),
            (workspace.APPROVED_STEP_IDS_ENV, json.dumps(approved_step_ids)),
            (workspace.APPROVED_SUCCESS_CRITERIA_ENV, json.dumps(approved_success_criteria)),
        ):
            self.patch.setenv(key, value)
        self.heartbeat()
        stop = json.loads(
            commands.run_playbook_command("action", f"aws ecs stop-task --task {TASK} --cluster demo --region {REGION}")
        )
        assert stop["ok"] is (self.mode != "failed-stop")
        # Observe the exact owner release separately from metric arithmetic.
        assert json.loads(commands.run_playbook_command("action", LOG_COMMAND))["ok"]
        if self.mode == "blocked-chain":
            before = len(self.spawned)
            denied = json.loads(commands.run_playbook_command("action", LOG_COMMAND + " && aws ecs stop-task"))
            assert denied["blocked"] and len(self.spawned) == before
        for command in (
            f"aws cloudwatch list-metrics --namespace Offline/Writes --region {REGION}",
            f"aws cloudwatch describe-alarms --alarm-names {ALARM['AlarmName']} --region {REGION}",
        ):
            assert json.loads(commands.run_playbook_command("action", command))["ok"]
        self.now = START + 121
        self.heartbeat()
        if self.mode != "failed-stop":
            self.wait_receipt = json.loads(
                commands.wait_for_post_action_metrics(
                    "verify", "action", METRICS, ALARM["AlarmName"], REGION, max_wait_seconds=12
                )
            )
            expected = {"unobservable": "UNOBSERVABLE", "unhealthy": "UNHEALTHY"}.get(self.mode, "HEALTHY")
            assert self.wait_receipt["status"] == expected, self.wait_receipt
        self.outcomes = [
            json.loads(
                commands.record_step_outcome(
                    step, criterion, "Local fake owner release and completed write observations", criteria_met=True
                )
            )
            for step, criterion in approved_success_criteria.items()
        ]
        if self.mode != "unconfirmed":
            # Deliberately optimistic model: the server must reject contradictory evidence.
            self.resolution_reply = json.loads(commands.record_resolution("Local recovery observations", True))
        self.records = self.work.read_records()
        return CodexResult(success=self.mode != "runner-failed", result="scripted offline response", raw_output="")

    def run_retrospective(self, prompt, **kwargs):
        self.retrospectives += 1
        assert self.get(f"EXEC#{EXECUTION}")["execution_state"]["S"] == "RESOLVED"
        assert PREFIX + "evidence.json" in self.s3.objects
        assert "unapproved-new-step" not in prompt
        if self.mode != "missing-attestation":
            assert json.loads(retrospective.save_playbook_update(json.dumps(self.update), RATIONALE))["ok"]
        return CodexResult(success=True, result="scripted offline retrospective", raw_output="")

    def publish_vectors(self, playbook, rca_id, *, metric_name, publication_id):
        assert rca_id == RCA and publication_id == EXECUTION
        assert self.work.path.is_dir()
        assert json.loads(self.s3.objects[PREFIX + "retrospective-diff.json"])["rationale"] == RATIONALE
        staged = self.get(STAGE)
        assert staged["publication_status"]["S"] == "PENDING"
        assert json.loads(staged["playbook"]["S"]) == playbook
        assert json.loads(self.get(REVISION)["playbook"]["S"]) == self.current
        self.events.append("vectors")
        self.vector_playbook = copy.deepcopy(playbook)
        return self.vector_success

    def process(self):
        assert ExecutionOrchestrator(self.container).process_message(json.dumps(self.approval))
        assert self.work is None or not self.work.path.exists()
        return self.get(f"EXEC#{EXECUTION}")

    def assert_durable_records(self, state):
        saved = json.loads(self.s3.objects[PREFIX + "evidence.json"])
        assert saved["final_state"] == state
        expected = assemble_evidence(self.records, execution_id=EXECUTION, rca_id=RCA, engine=ENGINE, playbook=PLAYBOOK)
        assert judge_resolution(expected, agent_succeeded=self.mode != "runner-failed").state == state
        expected_payload = expected.to_dict()
        for key in ("steps", "resolution_records", "metric_wait_records"):
            assert saved.get(key, []) == expected_payload.get(key, [])
        attempts = [a for s in saved["steps"] for a in s["attempts"]]
        raw_attempts = [r for r in self.records if r["type"] == "attempt"]
        assert len(attempts) == len(raw_attempts)
        assert saved.get("metric_wait_records", []) == [r for r in self.records if r["type"] == "metric_wait"]
        # Independent field comparison prevents reducer/serializer agreement from hiding loss.
        for raw, retained in zip(raw_attempts, attempts, strict=True):
            for key in ("command", "stdout", "stderr", "started_at", "ended_at", "recorded_at"):
                if key in raw:
                    assert retained[key] == raw[key]
        return saved


@pytest.fixture
def journey(monkeypatch, tmp_path):
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(workspace, "_WORKSPACE_ROOT", tmp_path / "executions")
    monkeypatch.setattr(s3_evidence_store, "S3_EVIDENCE_BUCKET", "offline-evidence")
    monkeypatch.setattr(dynamodb_execution_store, "DYNAMODB_TABLE_NAME", TABLE)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION, aws_access_key_id="offline", aws_secret_access_key="offline")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )
        yield Journey(monkeypatch, ddb)


@pytest.mark.parametrize(
    ("update", "status", "verification"),
    [
        ({}, "NO_CHANGE", "VERIFIED"),
        ({"temporary_mitigation": "Observed owner release"}, "UPDATED", "VERIFIED"),
        (
            {"execution_steps": [{"step_id": "verify", "action": "Also inspect the completed-write producer"}]},
            "UPDATED",
            "DRAFT",
        ),
    ],
    ids=["no-change", "metadata-update", "procedure-correction"],
)
def test_confirmed_recovery_preserves_approval_and_attestation_before_publication(
    journey, update, status, verification
):
    journey.update = update
    approved_bytes = journey.s3.objects[SNAPSHOT]
    item = journey.process()
    saved = journey.assert_durable_records("RESOLVED")
    assert item["retrospective_status"]["S"] == status
    assert item["playbook_snapshot_s3_key"]["S"] == SNAPSHOT
    assert journey.s3.objects[SNAPSHOT] == approved_bytes
    assert shlex.split(LOG_COMMAND) in journey.spawned
    assert saved["metric_wait_records"][-1]["status"] == "HEALTHY"
    diff = json.loads(journey.s3.objects[item["retrospective_diff_s3_key"]["S"]])
    assert diff["rationale"] == RATIONALE and len(diff["rationale"]) > 500
    assert diff["proposed_update"] == update and diff["update"] == update
    assert bool(diff["corrected_steps"]) is (verification == "DRAFT")
    assert journey.events == [PREFIX + "evidence.json", PREFIX + "retrospective-diff.json", "vectors"]
    published = journey.get(REVISION)
    assert published["publication_status"]["S"] == "PUBLISHED"
    assert published["revised_by_execution_id"]["S"] == EXECUTION
    assert json.loads(published["playbook"]["S"]) == journey.vector_playbook
    assert journey.vector_playbook["verification_status"] == verification
    assert [s["step_id"] for s in journey.vector_playbook["execution_steps"]] == ["action", "verify"]
    assert not journey.get(STAGE)
    before = (len(journey.spawned), journey.retrospectives, list(journey.events))
    journey.process()  # Redelivered approval cannot run commands or publish twice.
    assert (len(journey.spawned), journey.retrospectives, journey.events) == before


@pytest.mark.parametrize(
    "mode", ["unconfirmed", "runner-failed", "failed-stop", "blocked-chain", "unobservable", "unhealthy"]
)
def test_unconfirmed_or_contradicted_execution_keeps_evidence_without_promotion(journey, mode):
    journey.mode = mode
    item = journey.process()
    state = "FAILED" if mode == "runner-failed" else "UNRESOLVED"
    journey.assert_durable_records(state)
    assert item["execution_state"]["S"] == state
    assert "retrospective_status" not in item and journey.retrospectives == 0
    assert json.loads(journey.get(REVISION)["playbook"]["S"]) == journey.current
    assert not journey.get(STAGE)
    assert journey.events == [PREFIX + "evidence.json"]
    if mode in {"failed-stop", "blocked-chain", "unobservable", "unhealthy"}:
        assert not journey.resolution_reply["ok"]


@pytest.mark.parametrize("failure", ["evidence", "attestation", "missing-attestation", "vector"])
def test_recovery_survives_failed_retrospective_without_publication(journey, failure):
    journey.mode = "missing-attestation" if failure == "missing-attestation" else "confirmed"
    journey.s3.fail_suffix = {"evidence": "evidence.json", "attestation": "retrospective-diff.json"}.get(failure, "")
    journey.vector_success = failure != "vector"
    item = journey.process()
    assert item["execution_state"]["S"] == "RESOLVED"
    assert item["retrospective_status"]["S"] == "FAILED"
    assert json.loads(journey.get(REVISION)["playbook"]["S"]) == journey.current
    if failure == "evidence":
        assert journey.retrospectives == 0 and not journey.events
    else:
        journey.assert_durable_records("RESOLVED")
    if failure == "vector":
        assert journey.get(STAGE)["publication_status"]["S"] == "PENDING"
    else:
        assert "vectors" not in journey.events and not journey.get(STAGE)


def test_changed_snapshot_bytes_fail_digest_before_any_handler_or_publication(journey):
    journey.s3.objects[SNAPSHOT] = json.dumps(journey.current).encode()
    item = journey.process()
    assert item["execution_state"]["S"] == "FAILED"
    assert "digest" in item["error_reason"]["S"]
    assert not journey.spawned and not journey.events and journey.retrospectives == 0


def _crash_after_journal(journey, monkeypatch, *, negative=False, before_error=None, error=None):
    run = journey.run_execution

    def crash(*args, **kwargs):
        run(*args, **kwargs)
        if negative:
            assert json.loads(
                commands.record_step_outcome(
                    "verify",
                    PLAYBOOK["execution_steps"][1]["success_criteria"],
                    "WriteErrors observations unavailable",
                    criteria_met=False,
                    manual_action_required=True,
                )
            )["ok"]
            assert json.loads(
                commands.record_resolution("Recovery not observed", False, unobservable_reason="missing fixed bins")
            )["ok"]
        journey.records = journey.work.read_records()
        if before_error:
            before_error()
        raise error or OSError("runner transport failed password=do-not-retain")

    monkeypatch.setattr(journey, "run_execution", crash)


@pytest.mark.parametrize("mode", ["confirmed", "unconfirmed", "unobservable"])
def test_runner_exception_after_audited_commands_retains_evidence_before_cleanup(journey, monkeypatch, mode):
    journey.mode = mode
    _crash_after_journal(journey, monkeypatch, negative=mode == "unobservable")
    assert not ExecutionOrchestrator(journey.container).process_message(json.dumps(journey.approval))
    item = journey.get(f"EXEC#{EXECUTION}")
    assert item["execution_state"]["S"] == "FAILED"
    assert not journey.work.path.exists()
    assert any(r["type"] == "attempt" for r in journey.records)
    assert any(r["type"] == "metric_wait" for r in journey.records)
    assert journey.retrospectives == 0 and "vectors" not in journey.events
    # Read the persisted bytes through the same transport used by the S3 adapter.
    saved = json.loads(
        journey.s3.get_object(Bucket="offline-evidence", Key=item["evidence_s3_key"]["S"])["Body"].read()
    )
    assert saved["final_state"] == "FAILED"
    assert saved["error_reason"] == item["error_reason"]["S"]
    assert "OSError: runner transport failed" in saved["error_reason"]
    assert "do-not-retain" not in saved["error_reason"]
    assert "***REDACTED***" in saved["error_reason"]
    expected = assemble_evidence(journey.records, execution_id=EXECUTION, rca_id=RCA, engine=ENGINE, playbook=PLAYBOOK)
    for key in ("steps", "resolution_records", "metric_wait_records", "resolution_confirmed"):
        assert saved.get(key) == expected.to_dict().get(key)
    assert saved["metric_wait_records"] == [r for r in journey.records if r["type"] == "metric_wait"]
    assert timestamp(saved["started_at"]) <= timestamp(saved["ended_at"])
    assert [s["step_id"] for s in saved["steps"]] == ["action", "verify"]
    assert json.loads(journey.get(REVISION)["playbook"]["S"]) == journey.current
    if mode == "unconfirmed":
        assert saved["resolution_records"] == [] and saved["resolution_confirmed"] is None
    if mode == "unobservable":
        assert saved["steps"][1]["outcomes"][-1]["criteria_met"] is False
        assert saved["resolution_confirmed"] is False
    before = (len(journey.spawned), list(journey.events))
    journey.process()
    assert (len(journey.spawned), journey.events) == before


@pytest.mark.parametrize("failure", ["save", "state", "cleanup"])
def test_secondary_failure_does_not_mask_runner_error_or_publish(journey, monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise OSError("injected secondary failure")

    _crash_after_journal(journey, monkeypatch)
    if failure == "save":
        journey.s3.fail_suffix = "evidence.json"
    elif failure == "state":
        monkeypatch.setattr(journey.store, "update_state", unavailable)
    else:
        cleanup = workspace.ExecutionWorkspace.cleanup

        def cleanup_then_fail(work):
            cleanup(work)
            unavailable()

        monkeypatch.setattr(workspace.ExecutionWorkspace, "cleanup", cleanup_then_fail)
    with capture_logs() as logs:
        assert not ExecutionOrchestrator(journey.container).process_message(json.dumps(journey.approval))
    item = journey.get(f"EXEC#{EXECUTION}")
    assert item["execution_state"]["S"] == ("EXECUTING" if failure == "state" else "FAILED")
    assert journey.retrospectives == 0 and "vectors" not in journey.events
    original = next(log for log in logs if log["event"] == "execution_pipeline_failed")
    assert "runner transport failed" in original["detail"] and "do-not-retain" not in original["detail"]
    if failure != "state":
        assert item["error_reason"]["S"] == original["detail"]
    if failure == "save":
        assert "evidence_s3_key" not in item and PREFIX + "evidence.json" not in journey.s3.objects
    else:
        saved = json.loads(journey.s3.objects[PREFIX + "evidence.json"])
        assert saved["final_state"] == "FAILED" and saved["error_reason"] == original["detail"]
    assert not journey.work.path.exists()


@pytest.mark.parametrize("fence", ["replaced", "expired", "unavailable", "explicit-claim-lost"])
def test_exception_recovery_never_writes_when_claim_is_lost_or_unobservable(journey, monkeypatch, fence):
    def fence_after_commands():
        if fence in {"replaced", "expired"}:
            field, value = (
                ("claim_token", {"S": "new-owner"}) if fence == "replaced" else ("claim_expires_at", {"N": "0"})
            )
            journey.ddb.update_item(
                TableName=TABLE,
                Key={"PK": {"S": f"RCA#{RCA}"}, "SK": {"S": f"EXEC#{EXECUTION}"}},
                UpdateExpression=f"SET {field} = :value",
                ExpressionAttributeValues={":value": value},
            )
        elif fence == "unavailable":

            def unavailable(*args, **kwargs):
                raise OSError("claim read unavailable")

            monkeypatch.setattr(journey.store, "is_execution_current", unavailable)
        journey.s3.objects[PREFIX + "evidence.json"] = b'{"other_owner":"must survive"}'
        journey.fenced_item = journey.get(f"EXEC#{EXECUTION}")

    _crash_after_journal(
        journey,
        monkeypatch,
        before_error=fence_after_commands,
        error=ExecutionClaimLostError("lost execution claim") if fence == "explicit-claim-lost" else None,
    )
    assert not ExecutionOrchestrator(journey.container).process_message(json.dumps(journey.approval))
    assert journey.get(f"EXEC#{EXECUTION}") == journey.fenced_item
    assert journey.s3.objects[PREFIX + "evidence.json"] == b'{"other_owner":"must survive"}'
    assert not journey.events and journey.retrospectives == 0
    assert not journey.work.path.exists()


@pytest.mark.parametrize("phase", ["create", "prepare", "context"])
def test_early_setup_exception_is_failed_without_inventing_execution_records(journey, monkeypatch, phase):
    def fail_create(*args, **kwargs):
        raise OSError("workspace allocation failed")

    if phase == "create":
        monkeypatch.setattr(workspace.ExecutionWorkspace, "create", fail_create)
    else:
        prepare = workspace.ExecutionWorkspace.prepare

        def fail_setup(work, **kwargs):
            journey.work = work
            if phase == "prepare":
                prepare(work)
            raise OSError("setup failed before runner")

        monkeypatch.setattr(
            workspace.ExecutionWorkspace, "prepare" if phase == "prepare" else "write_observation_context", fail_setup
        )
    assert not ExecutionOrchestrator(journey.container).process_message(json.dumps(journey.approval))
    item = journey.get(f"EXEC#{EXECUTION}")
    assert item["execution_state"]["S"] == "FAILED"
    assert not journey.spawned and journey.retrospectives == 0
    assert journey.work is None or not journey.work.path.exists()
    if phase == "context":
        saved = json.loads(journey.s3.objects[item["evidence_s3_key"]["S"]])
        assert saved["final_state"] == "FAILED"
        assert "started_at" not in saved and "ended_at" not in saved
        assert saved["resolution_records"] == [] and saved["resolution_confirmed"] is None
        assert all(s["attempts"] == [] and s["outcomes"] == [] for s in saved["steps"])
    else:
        assert not journey.events and "evidence_s3_key" not in item


def test_unchanged_customer_archive_roundtrips_all_records_without_claiming_live_success(journey):
    """Historical replay checks retained data, not handler calls or today's live state."""
    path = Path(__file__).parent / "fixtures/retrospective-custlock-20260910.json"
    raw = path.read_bytes()
    archive = json.loads(raw)
    source = archive["evidence"]
    records = [{"type": "attempt", **attempt} for step in source["steps"] for attempt in step["attempts"]]
    records.extend(
        {"type": "step_outcome", "step_id": step["step_id"], **outcome}
        for step in source["steps"]
        for outcome in step["outcomes"]
    )
    records.extend({"type": "resolution", **record} for record in source["resolution_records"])
    records.extend(source["metric_wait_records"])
    evidence = assemble_evidence(
        records,
        execution_id=source["execution_id"],
        rca_id=source["rca_id"],
        engine=source["engine"],
        playbook=archive["playbook_before"],
        started_at=source.get("started_at"),
        ended_at=source.get("ended_at"),
    )
    journey.work = workspace.ExecutionWorkspace.create("archive-offline-replay")
    journey.work.prepare()
    try:
        key = journey.container.evidence_store.save_execution_evidence(
            evidence.execution_id, rca_id=evidence.rca_id, evidence=evidence.to_dict()
        )
    finally:
        journey.work.cleanup()
    saved = json.loads(journey.s3.objects[key])
    assert sum(len(s["attempts"]) for s in saved["steps"]) == 34
    assert saved["metric_wait_records"] == source["metric_wait_records"]
    assert len(saved["metric_wait_records"]) == 20
    assert saved["resolution_records"] == source["resolution_records"]
    assert archive["provenance"]["archived_retrospective_status"] == "FAILED"
    assert path.read_bytes() == raw
    assert not journey.spawned and journey.retrospectives == 0
