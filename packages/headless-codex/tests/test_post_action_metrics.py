"""Offline B2 contract tests: clocks, waits, and CLI clients are injected."""

import copy
import json
import shlex
import subprocess
from unittest.mock import Mock

import pytest

from headless_codex import execution_mcp_server as server
from headless_codex.services import execution_workspace as wm
from headless_codex.services.command_gate import GateVerdict, evaluate_command
from headless_codex.services.execution_contract import command_digest
from headless_codex.services.execution_outcome import assemble_evidence, judge_resolution
from headless_codex.services.execution_state import ExecutionState
from headless_codex.services.post_action_metrics import (
    ObservationBudget,
    ObservationStoppedError,
    assess_metric,
    bind_request,
    normalize_request,
    poll_fixed_metrics,
    timestamp,
    utc,
)

STOP = timestamp("2026-09-10T12:34:07.435002Z")
START = timestamp("2026-09-10T12:35:00Z")
END = START + 120
REGION = "us-east-1"
ACCOUNT = "123456789012"
TASK = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/cluster/owned"
CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/cluster"


class Clock:
    def __init__(self, now=END + 1):
        self.now, self.waits = now, []
        self.cancel_at, self.deadline = float("inf"), now + 1000

    def time(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def control(self):
        if self.now >= self.cancel_at:
            raise ObservationStoppedError("cancelled or claim lost")
        return self.deadline

    def budget(self, seconds=300):
        return ObservationBudget(
            min(self.now + seconds, self.deadline), self.control, clock=self.time, monotonic=self.time, wait=self.wait
        )


def attempt(command, stdout, step_id="verify", **extra):
    return {
        "type": "attempt",
        "execution_id": "exec-1",
        "step_id": step_id,
        "command": command,
        "command_digest": command_digest(command),
        "command_index": 0,
        "stdout": json.dumps(stdout),
        "succeeded": True,
        "exit_status": "0",
        "ended_at": utc(STOP),
        **extra,
    }


@pytest.fixture
def data():
    metrics = {
        role: {"namespace": "Product/Requests", "metric_name": name, "dimensions": {"Service": "observed-api"}}
        for role, name in {"attempts": "WriteCompletions", "failures": "WriteErrors"}.items()
    }
    alarm = {
        "AlarmName": "observed-errors",
        "AlarmArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:observed-errors",
        "Namespace": "Product/Requests",
        "MetricName": "WriteErrors",
        "Dimensions": [{"Name": "Service", "Value": "observed-api"}],
        "Period": 60,
        "Statistic": "Sum",
        "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "StateValue": "OK",
    }
    steps = [
        {"step_id": s, "success_criteria": c}
        for s, c in [
            ("precheck", "same owner"),
            ("action", "stop exact task"),
            ("verify", "WriteErrors Sum=0, real writes and observed-errors OK in first two full bins"),
            ("later", "later action"),
        ]
    ]
    context = {"alarm_name": "observed-errors", "alarm_data": {}, "playbook": {"execution_steps": steps}}
    records = [
        attempt(
            f"aws ecs stop-task --task {TASK} --cluster cluster --region {REGION}",
            {"task": {"taskArn": TASK, "clusterArn": CLUSTER}},
            "action",
        ),
        attempt(
            f"aws cloudwatch list-metrics --namespace Product/Requests --region {REGION}",
            {
                "Metrics": [
                    {"Namespace": m["namespace"], "MetricName": m["metric_name"], "Dimensions": alarm["Dimensions"]}
                    for m in metrics.values()
                ]
            },
        ),
        attempt(
            f"aws cloudwatch describe-alarms --alarm-names observed-errors --region {REGION}", {"MetricAlarms": [alarm]}
        ),
    ]
    records[1]["step_id"] = records[2]["step_id"] = "precheck"
    records[2]["command_index"] = 1
    steps[0]["commands"] = [r["command"] for r in records[1:]]
    steps[1]["commands"] = [records[0]["command"]]
    steps[3]["commands"] = ["aws ecs describe-clusters --region us-east-1"]
    request = normalize_request("verify", "action", metrics, "observed-errors", "", REGION, 300)
    steps[2]["metric_wait"] = {k: v for k, v in request.items() if k != "step_id"}
    bound = {**bind_request(request, records, context, "exec-1", evaluate_command), "request": request}
    return {
        "metrics": metrics,
        "alarm": alarm,
        "context": context,
        "records": records,
        "request": request,
        "bound": bound,
    }


def response(data, command, *, points=None, value=None):
    args = shlex.split(command)
    if args[2] == "describe-alarms":
        payload = {"MetricAlarms": list(data["bound"]["alarms"].values())}
    else:
        name = args[args.index("--metric-name") + 1]
        role = next(k for k, v in data["metrics"].items() if v["metric_name"] == name)
        stat = data["bound"]["alarms"]["latency"]["Statistic"] if role == "latency" else "Sum"
        if points is None:
            points = [
                {
                    "Timestamp": utc(t),
                    stat: (0 if role == "failures" else 12) if value is None else value,
                    "SampleCount": 2,
                    "Unit": "Milliseconds" if role == "latency" else "Count",
                }
                for t in (START, START + 60)
            ]
        payload = {"Label": name, "Datapoints": points}
    return {"ok": True, "stdout": json.dumps(payload), "stderr": ""}


def run(data, clock=None, client=None, seconds=300):
    clock, history, commands = clock or Clock(), [], []

    def execute(command, budget):
        budget.remaining()
        commands.append(command)
        return client(command, len(commands)) if client else response(data, command)

    result = poll_fixed_metrics(data["bound"], clock.budget(seconds), execute, history.append)
    return result, history, commands


def test_normal_plan_needs_no_latency_or_semantic_descriptor(data):
    result, history, commands = run(data)
    assert result["status"] == "HEALTHY"
    assert [b["start"] for b in result["bins"]] == [utc(START), utc(START + 60)]
    assert [b["attempts_minus_failures"] for b in result["bins"]] == [12, 12]
    assert not result["write_semantics_verified"]
    assert all("successful_writes" not in b and "query_latency" not in b for b in result["bins"])
    assert "not successful-write proof" in result["guidance"]
    assert len(commands) == 3 and len(history) == 4


def test_exact_boundary_still_uses_next_minute(data):
    data["records"][0]["ended_at"] = utc(START)
    bound = bind_request(data["request"], data["records"], data["context"], "exec-1", evaluate_command)
    assert bound["start"] == utc(START + 60) and bound["end"] == utc(START + 180)


def test_early_alarm_late_arrivals_keep_first_bins(data):
    clock = Clock(END - 27)

    def client(command, _):
        payload = response(data, command)
        if "get-metric-statistics" in command and END <= clock.now < END + 8:
            parsed = json.loads(payload["stdout"])
            parsed["Datapoints"] = parsed["Datapoints"][:1]
            payload["stdout"] = json.dumps(parsed)
        return payload

    result, history, commands = run(data, clock, client)
    assert result["ok"] and clock.now >= END + 8
    assert clock.waits and max(clock.waits) <= 0.5
    for command in commands:
        if "get-metric-statistics" in command:
            args = shlex.split(command)
            assert args[args.index("--start-time") + 1] == utc(START)
            assert args[args.index("--end-time") + 1] == utc(END)
    assert len(history) > 4


@pytest.mark.parametrize("partial", [False, True])
def test_missing_and_partial_not_zero(data, partial):
    def client(command, _):
        if "--metric-name WriteErrors" in command:
            points = [{"Timestamp": utc(START), "SampleCount": 2, "Unit": "Count"}] if partial else []
            return response(data, command, points=points)
        return response(data, command)

    result, history, _ = run(data, client=client, seconds=12)
    assert result["status"] == "UNOBSERVABLE"
    assert any(r.get("role") == "failures" for r in history)


def test_missing_sibling_complete_bad_failure_is_sticky_before_retry(data):
    def client(command, _):
        if "WriteCompletions" in command:
            return response(data, command, points=[])
        return response(data, command, value=1)

    result, history, commands = run(data, client=client)
    assert result["status"] == "UNHEALTHY" and len(commands) == 2
    assert "nonzero" in result["failures"][0]
    assert len([r for r in history if r["phase"] == "poll"]) == 2


def test_completeness_uses_each_command_end_not_poll_start(data):
    clock = Clock(END - 1)

    def client(command, _):
        clock.now += 2
        return response(data, command, value=1 if "WriteErrors" in command else None)

    result, history, _ = run(data, clock, client)
    assert result["status"] == "UNHEALTHY"
    assert timestamp(history[0]["observed_at"]) == END + 1


def test_query_error_output_preserved_after_recovery(data):
    def client(command, n):
        if n == 1:
            return {"ok": False, "exit_status": 255, "stdout": '{"partial":', "stderr": "throttled id xyz"}
        return response(data, command)

    result, history, _ = run(data, client=client)
    assert result["ok"]
    assert history[0]["response"]["stdout"] == '{"partial":'
    assert "throttled" in history[0]["response"]["stderr"] and history[0]["observed_at"]


@pytest.mark.parametrize("kind", ["nan", "infinity", "duplicate", "misaligned", "negative", "boolean"])
def test_invalid_datapoints_fail(data, kind):
    points = [{"Timestamp": utc(START), "Sum": 0, "SampleCount": 2, "Unit": "Count"}]
    if kind == "duplicate":
        points.append(copy.deepcopy(points[0]))
    elif kind == "misaligned":
        points[0]["Timestamp"] = utc(START + 1)
    else:
        points[0]["Sum"] = {"nan": float("nan"), "infinity": float("inf"), "negative": -1, "boolean": False}[kind]
    _, errors = assess_metric(
        "failures",
        response(data, "aws cloudwatch get-metric-statistics --metric-name WriteErrors", points=points),
        data["bound"],
        END,
    )
    assert errors


def test_prewindow_and_current_bins_excluded(data):
    points = [
        {"Timestamp": utc(t), "Sum": 0, "SampleCount": 2, "Unit": "Count"} for t in (START - 60, START, START + 60, END)
    ]
    values, errors = assess_metric(
        "failures",
        response(data, "aws cloudwatch get-metric-statistics --metric-name WriteErrors", points=points),
        data["bound"],
        END - 1,
    )
    assert not errors and set(values) == {utc(START)}


def test_budget_and_cancel_claim_do_not_extend_wait(data):
    clock = Clock()
    clock.cancel_at = clock.now + 1.25
    result, _, _ = run(data, clock, lambda c, _: response(data, c, points=[]))
    assert result["status"] == "UNOBSERVABLE" and "claim lost" in result["error"]
    assert clock.now <= clock.cancel_at + 0.5
    clock = Clock()
    original = clock.now
    clock.deadline = original + 3
    result, _, _ = run(data, clock, lambda c, _: response(data, c, points=[]))
    assert result["status"] == "UNOBSERVABLE" and clock.now <= original + 3


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "read_only",
        "other_execution",
        "other_step",
        "future_step",
        "unapproved",
        "other_region",
        "wrong_task",
        "missing_time",
    ],
)
def test_invalid_action_anchor_rejected(data, change):
    anchor, request = data["records"][0], data["request"]
    if change == "failed":
        anchor["succeeded"] = False
    elif change == "read_only":
        anchor["command"] = anchor["command"].replace("stop-task", "describe-tasks")
    elif change == "other_execution":
        anchor["execution_id"] = "previous-execution"
    elif change == "other_step":
        anchor["step_id"] = "precheck"
    elif change == "future_step":
        request["action_step_id"] = "later"
    elif change == "unapproved":
        request["action_step_id"] = "unknown"
    elif change == "other_region":
        request["region"] = "eu-west-1"
    elif change == "wrong_task":
        anchor["stdout"] = json.dumps({"task": {"taskArn": TASK + "other", "clusterArn": CLUSTER}})
    elif change == "missing_time":
        del anchor["ended_at"]
    with pytest.raises(ValueError):
        bind_request(request, data["records"], data["context"], "exec-1", evaluate_command)


def test_later_read_and_successful_retry_never_rebase(data):
    retry = copy.deepcopy(data["records"][0])
    retry["ended_at"] = utc(END)
    data["records"].extend(
        [
            retry,
            attempt(f"aws ecs describe-tasks --tasks {TASK} --region {REGION}", {}, "action", ended_at=utc(END + 100)),
        ]
    )
    bound = bind_request(data["request"], data["records"], data["context"], "exec-1", evaluate_command)
    assert bound["anchor"]["ended_at"] == utc(STOP) and bound["start"] == utc(START)


def add_latency(data, approved=True):
    metric = {**data["metrics"]["attempts"], "metric_name": "ObservedReadLatency"}
    data["metrics"]["latency"] = metric
    alarm = {
        **data["alarm"],
        "MetricName": "ObservedReadLatency",
        "AlarmName": "observed-latency",
        "AlarmArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:observed-latency",
        "Statistic": "Average",
        "Threshold": 20,
        "Unit": "Milliseconds",
    }
    data["records"].append(
        attempt(
            f"aws cloudwatch describe-alarms --alarm-names observed-latency --region {REGION}",
            {"MetricAlarms": [alarm]},
        )
    )
    data["records"][-1]["step_id"] = "precheck"
    data["records"][-1]["command_index"] = 2
    data["context"]["playbook"]["execution_steps"][0]["commands"].append(data["records"][-1]["command"])
    if approved:
        data["context"]["playbook"]["execution_steps"][2]["success_criteria"] += " ObservedReadLatency observed-latency"
    request = normalize_request("verify", "action", data["metrics"], "observed-errors", "observed-latency", REGION, 300)
    if approved:
        data["context"]["playbook"]["execution_steps"][2]["metric_wait"] = {
            k: v for k, v in request.items() if k != "step_id"
        }
    data["bound"] = {
        **bind_request(request, data["records"], data["context"], "exec-1", evaluate_command),
        "request": request,
    }


def test_unapproved_latency_rejected(data):
    with pytest.raises(ValueError, match="exactly match"):
        add_latency(data, approved=False)


@pytest.mark.parametrize("value, expected", [(12, "HEALTHY"), (20, "UNHEALTHY"), (21, "UNHEALTHY")])
def test_optional_latency_uses_actual_alarm_threshold(data, value, expected):
    add_latency(data)
    result, _, _ = run(
        data, client=lambda c, _: response(data, c, value=value if "--metric-name ObservedReadLatency" in c else None)
    )
    assert result["status"] == expected


def test_optional_query_requires_real_sample_count(data):
    add_latency(data)

    def client(c, _):
        if "--metric-name ObservedReadLatency" in c:
            return response(
                data, c, points=[{"Timestamp": utc(START), "Average": 2, "SampleCount": 0, "Unit": "Milliseconds"}]
            )
        return response(data, c)

    result, _, _ = run(data, client=client)
    assert result["status"] == "UNHEALTHY"


def test_completed_write_descriptor_binds_actual_source_counts(data):
    descriptor = {
        "namespace": data["metrics"]["attempts"]["namespace"],
        "dimensions": data["metrics"]["attempts"]["dimensions"],
        "attempts_metric": "WriteCompletions",
        "failures_metric": "WriteErrors",
        "operation_kind": "write",
        "accounting": "completed",
    }
    data["records"].append(
        attempt(
            f"aws logs get-log-events --log-group-name observed --log-stream-name producer --region {REGION}",
            {"events": [{"message": json.dumps({"accounting": descriptor})}]},
        )
    )
    data["records"][-1]["step_id"] = "precheck"
    data["records"][-1]["command_index"] = 2
    data["context"]["playbook"]["execution_steps"][0]["commands"].append(data["records"][-1]["command"])
    data["request"]["completed_work_evidence"] = {"record_index": 3, "json_pointer": "/events/0/message/accounting"}
    data["context"]["playbook"]["execution_steps"][2]["metric_wait"]["completed_work_evidence"] = data["request"][
        "completed_work_evidence"
    ]
    from headless_codex.services.execution_contract import validate_steps

    validate_steps(data["context"]["playbook"])
    bound = bind_request(data["request"], data["records"], data["context"], "exec-1", evaluate_command)
    data["bound"] = {**bound, "request": data["request"]}
    result, _, _ = run(data)
    assert result["write_semantics_verified"] and [b["successful_writes"] for b in result["bins"]] == [12, 12]
    descriptor["accounting"] = "started"
    data["records"][-1]["stdout"] = json.dumps({"events": [{"message": json.dumps({"accounting": descriptor})}]})
    with pytest.raises(ValueError, match="completed-write"):
        bind_request(data["request"], data["records"], data["context"], "exec-1", evaluate_command)


@pytest.fixture
def mcp_workspace(data, monkeypatch, tmp_path):
    monkeypatch.setattr(wm, "_WORKSPACE_ROOT", tmp_path / "executions")
    workspace = wm.ExecutionWorkspace.create("exec-1")
    workspace.prepare()
    monkeypatch.setenv(wm.EXECUTION_TOKEN_ENV, workspace.token)
    monkeypatch.setenv(wm.EXECUTION_ID_ENV, "exec-1")
    steps = data["context"]["playbook"]["execution_steps"]
    monkeypatch.setenv(wm.APPROVED_STEP_IDS_ENV, json.dumps([s["step_id"] for s in steps]))
    monkeypatch.setenv(
        wm.APPROVED_SUCCESS_CRITERIA_ENV, json.dumps({s["step_id"]: s["success_criteria"] for s in steps})
    )
    workspace.write_observation_context(**data["context"])
    for record in [*data["records"][1:], data["records"][0]]:
        server._append_record(record)
    clock, calls = Clock(), []
    monkeypatch.setattr(server.time, "time", clock.time)
    monkeypatch.setattr(server, "_observation_control", clock.control)
    monkeypatch.setattr(server, "ObservationBudget", lambda deadline, _: clock.budget(deadline - clock.now))

    def execute(argv, budget):
        assert any(r.get("phase") == "started" for r in workspace.read_records())
        budget.remaining()
        calls.append(argv)
        result = response(data, shlex.join(argv))
        return subprocess.CompletedProcess(argv, 0, result["stdout"], result["stderr"])

    monkeypatch.setattr(server, "_run_observation_process", execute)
    return workspace, clock, calls


def invoke(data, **overrides):
    args = {
        "step_id": "verify",
        "action_step_id": "action",
        "metrics": data["metrics"],
        "failure_alarm_name": "observed-errors",
        "region": REGION,
        "max_wait_seconds": data["request"]["max_wait_seconds"],
    }
    return json.loads(server.wait_for_post_action_metrics(**{**args, **overrides}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("action_step_id", "precheck"),
        ("failure_alarm_name", "another-alarm"),
        ("region", "us-west-2"),
        ("max_wait_seconds", 299),
        ("completed_work_evidence", {"record_index": "approved_context", "json_pointer": "/new"}),
    ],
)
def test_changed_approved_wait_argument_is_rejected_before_any_cloudwatch_read(data, mcp_workspace, field, value):
    workspace, _, calls = mcp_workspace
    reply = invoke(data, **{field: value})
    assert not reply["ok"]
    assert "exactly match" in reply["error"]
    assert calls == []
    assert workspace.read_records()[-1]["type"] == "approval_rejection"


@pytest.mark.parametrize(
    "coordinate,value",
    [("metric_name", "Other"), ("namespace", "Other/Namespace"), ("dimensions", {"Service": "other"})],
)
def test_changed_wait_metric_coordinate_is_rejected_before_binding(data, mcp_workspace, coordinate, value):
    _, _, calls = mcp_workspace
    metrics = copy.deepcopy(data["metrics"])
    for metric in metrics.values():
        if coordinate == "metric_name":
            metric[coordinate] += value
        else:
            metric[coordinate] = value
    assert not invoke(data, metrics=metrics)["ok"]
    assert calls == []


def test_general_command_tool_cannot_execute_a_waiters_generated_read(data, mcp_workspace):
    workspace, _, calls = mcp_workspace
    assert invoke(data)["ok"]
    generated = next(r["command"] for r in workspace.read_records() if r.get("internal_observation"))
    reply = json.loads(server.run_playbook_command("verify", generated))
    assert not reply["ok"] and "exactly match" in reply["error"]
    assert len(calls) == 3


def test_missing_approved_discovery_produces_sticky_unobservable_receipt_without_running_reads(data, mcp_workspace):
    workspace, _, calls = mcp_workspace
    # A completed but truncated discovery is an attempt, never binding authority.
    records = workspace.read_records()
    for record in records:
        if record.get("step_id") == "precheck":
            record["stdout_truncated"] = True
    workspace.evidence_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    first = invoke(data)
    assert first["status"] == "UNOBSERVABLE"
    assert "discovery" in first["error"]
    assert invoke(data)["status"] == "UNOBSERVABLE"
    assert calls == []
    assert len([r for r in workspace.read_records() if r.get("phase") == "terminal"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["300", 300.0, True])
async def test_mcp_does_not_coerce_wait_duration_before_approval_comparison(data, mcp_workspace, value):
    from fastmcp.exceptions import ValidationError

    _, _, calls = mcp_workspace
    with pytest.raises(ValidationError):
        await server.mcp.call_tool(
            "wait_for_post_action_metrics",
            {
                "step_id": "verify",
                "action_step_id": "action",
                "metrics": data["metrics"],
                "failure_alarm_name": "observed-errors",
                "region": REGION,
                "max_wait_seconds": value,
            },
        )
    assert calls == []


def test_mcp_persist_first_and_repeat_terminal_no_reset(data, mcp_workspace):
    workspace, _, calls = mcp_workspace
    first = invoke(data)
    assert first["ok"] and len(calls) == 3
    second = invoke(data)
    assert second["bins"] == first["bins"] and second["binding"] == first["binding"]
    assert not invoke(data, max_wait_seconds=20)["ok"]
    assert not invoke(data, action_step_id="precheck")["ok"]
    assert len(calls) == 3
    evidence = assemble_evidence(
        workspace.read_records(),
        execution_id="exec-1",
        rca_id="rca",
        engine="headless-codex",
        playbook=data["context"]["playbook"],
    )
    assert len(evidence.to_dict()["metric_wait_records"]) == 5
    assert len(evidence.step("verify").attempts) == 3


def test_interrupted_wait_never_restarts(data, mcp_workspace):
    _, _, calls = mcp_workspace
    server._append_record({"type": "metric_wait", "phase": "started", "step_id": "verify", "binding": data["bound"]})
    result = invoke(data)
    assert not result["ok"] and "interrupted" in result["error"] and not calls
    assert invoke(data)["status"] == "UNOBSERVABLE"


def test_cli_gate_still_enforced(data, mcp_workspace, monkeypatch):
    _, _, calls = mcp_workspace

    def gate(command):
        if "get-metric-statistics" in command:
            return GateVerdict(False, "test gate refusal", undecidable=True)
        return evaluate_command(command)

    monkeypatch.setattr(server, "evaluate_command", gate)
    result = invoke(data)
    assert result["status"] == "UNHEALTHY" and not calls


def test_failed_wait_cannot_be_overwritten_by_model(data, mcp_workspace):
    workspace, _, _ = mcp_workspace
    server._append_record({"type": "metric_wait", "phase": "terminal", "step_id": "verify", "status": "UNHEALTHY"})
    criterion = data["context"]["playbook"]["execution_steps"][2]["success_criteria"]
    assert not json.loads(server.record_step_outcome("verify", criterion, "now green", True))["ok"]
    assert not json.loads(server.record_resolution("now green", True))["ok"]
    evidence = assemble_evidence(
        workspace.read_records(),
        execution_id="exec-1",
        rca_id="rca",
        engine="headless-codex",
        playbook=data["context"]["playbook"],
    )
    evidence.resolution_confirmed, evidence.resolution_observation = True, "now green"
    assert judge_resolution(evidence, agent_succeeded=True).state == ExecutionState.UNRESOLVED


def test_cli_cancel_kills_process_and_retains_partial_output(monkeypatch):
    clock, proc = Clock(), Mock()
    clock.cancel_at = clock.now + 0.5

    def communicate(timeout=None):
        if timeout is not None:
            clock.now += timeout
            raise subprocess.TimeoutExpired("aws", timeout, output="partial")
        return "partial", "cancelled output"

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(server.subprocess, "Popen", Mock(return_value=proc))
    with pytest.raises(ObservationStoppedError) as caught:
        server._run_observation_process(("aws", "cloudwatch", "describe-alarms"), clock.budget())
    proc.kill.assert_called_once()
    assert caught.value.stdout == "partial"


@pytest.mark.parametrize(
    "flag",
    [
        "--generate-cli-skeleton output",
        "--query task",
        "--profile other",
        "--endpoint-url https://example.invalid",
        '--cli-input-json "{}"',
    ],
)
def test_synthetic_or_alternate_scope_action_cannot_anchor(data, flag):
    data["records"][0]["command"] += " " + flag
    with pytest.raises(ValueError):
        bind_request(data["request"], data["records"], data["context"], "exec-1", evaluate_command)


@pytest.mark.parametrize(
    "field,value",
    [("AWSAccountId", "999999999999"), ("AlarmArn", "arn:aws:cloudwatch:eu-west-1:123456789012:alarm:observed-errors")],
)
def test_action_must_match_original_incident_scope(data, field, value):
    data["context"]["alarm_data"][field] = value
    with pytest.raises(ValueError, match="context|incident"):
        bind_request(data["request"], data["records"], data["context"], "exec-1", evaluate_command)


def test_cli_error_with_complete_bad_datapoint_still_fails_sticky(data):
    def client(command, _):
        if "WriteCompletions" in command:
            return response(data, command, points=[])
        return {**response(data, command, value=1), "ok": False, "exit_status": 255, "stderr": "partial query error"}

    result, history, commands = run(data, client=client)
    assert result["status"] == "UNHEALTHY" and len(commands) == 2
    assert history[-2]["response"]["stderr"] == "partial query error"


def test_stale_control_rejected_by_actual_reader(monkeypatch, tmp_path):
    from headless_codex.services.post_action_metrics import ObservationStoppedError

    monkeypatch.setattr(wm, "_WORKSPACE_ROOT", tmp_path / "executions")
    workspace = wm.ExecutionWorkspace.create("exec-1")
    workspace.prepare()
    monkeypatch.setenv(wm.EXECUTION_TOKEN_ENV, workspace.token)
    monkeypatch.setenv(wm.EXECUTION_ID_ENV, "exec-1")
    monkeypatch.setattr(server.time, "time", lambda: 1011)
    wm.write_observation_json(
        wm.observation_control_path_for_token(workspace.token),
        {"execution_id": "exec-1", "active": True, "checked_at_epoch": 1000, "deadline_epoch": 1030},
    )
    with pytest.raises(ObservationStoppedError, match="stale"):
        server._observation_control()


def test_running_query_and_cleanup_stay_within_remaining_budget(monkeypatch):
    clock, proc = Clock(), Mock()
    started = clock.now

    def communicate(timeout=None):
        assert timeout is not None and 0 <= timeout <= 0.5
        clock.now += timeout
        raise subprocess.TimeoutExpired("aws", timeout, output="partial response")

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(server.subprocess, "Popen", Mock(return_value=proc))
    with pytest.raises(ObservationStoppedError, match="command deadline"):
        server._run_observation_process(("aws", "cloudwatch", "describe-alarms"), clock.budget(3))
    proc.kill.assert_called_once()
    assert clock.now - started <= 3


def test_precompletion_wait_checks_cancel_without_unusable_queries(data):
    clock = Clock(STOP + 1)
    clock.cancel_at = clock.now + 0.5
    result, _, commands = run(data, clock)
    assert result["status"] == "UNOBSERVABLE" and not commands
    assert clock.now <= clock.cancel_at + 0.5


def test_wait_output_projection_is_bounded_without_changing_durable_history():
    from headless_codex.services.execution_evidence import ExecutionEvidence, retrospective_evidence_json

    evidence = ExecutionEvidence(execution_id="exec", rca_id="rca", playbook_id="pb")
    evidence.metric_wait_records = [
        {
            "type": "metric_wait",
            "phase": "poll",
            "step_id": "verify",
            "observed_at": utc(END + i),
            "response": {"ok": False, "stdout": "partial output " * 1000, "stderr": "query error"},
        }
        for i in range(10)
    ]
    before = copy.deepcopy(evidence.to_dict())
    projected = retrospective_evidence_json(evidence, max_chars=10_000)
    assert len(projected) <= 10_000
    assert json.loads(projected)["projection"]["output_previews_omitted"]
    assert evidence.to_dict() == before
