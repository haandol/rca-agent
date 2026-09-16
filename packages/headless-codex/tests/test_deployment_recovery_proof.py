"""Reject demonstrated false resolutions through production public tools."""

import json
import subprocess

import pytest
from test_service_deployment import (
    NEW_TASK,
    accounting_context,
)
from test_service_deployment import (
    test_public_rollback_convergence_and_metric_tools_share_immutable_anchor as run_full_tools,
)

from headless_codex import execution_mcp_server as server
from headless_codex.services.execution_contract import validate_steps
from headless_codex.services.execution_outcome import judge_resolution
from headless_codex.services.post_action_metrics import timestamp
from headless_codex.services.runbook_contract import validate_runbook

pytest_plugins = ["test_service_deployment"]


@pytest.mark.parametrize("mutation", ["missing", "before", "stop_anchor", "foreign_anchor", "foreign_region"])
def test_incomplete_recovery_refused_before_any_write(plan, runtime, mutation):
    """A guarded rollback cannot use an absent or unrelated recovery step."""
    workspace, ecs = runtime
    if mutation == "missing":
        plan["execution_steps"].pop()
    elif mutation == "before":
        plan["execution_steps"].insert(1, plan["execution_steps"].pop())
    elif mutation == "stop_anchor":
        wait = plan["execution_steps"][2]["metric_wait"]
        wait["action_step_id"] = wait.pop("deployment_step_id")
    elif mutation == "foreign_anchor":
        plan["execution_steps"][2]["metric_wait"]["deployment_step_id"] = "other"
    else:
        plan["execution_steps"][2]["metric_wait"]["region"] = "us-west-2"
    for validate in (lambda: validate_runbook(plan["execution_steps"]), lambda: validate_steps(plan)):
        with pytest.raises(ValueError):
            validate()
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="observed-errors")
    assert not json.loads(server.run_playbook_command("rollback", plan["execution_steps"][0]["commands"][0]))["ok"]
    assert ecs.commands == []


def record_outcomes(plan):
    """Exercise model-callable outcomes instead of fabricating server records."""
    return [
        json.loads(server.record_step_outcome(s["step_id"], s["success_criteria"], "Observed approved results", True))
        for s in plan["execution_steps"]
    ]


def test_arithmetic_only_receipt_and_model_declaration_cannot_resolve(plan, runtime, metric_data, monkeypatch):
    """E2: public receipts without write semantics stay unresolved."""
    run_full_tools(plan, runtime, metric_data, monkeypatch)
    workspace, _ = runtime
    terminal = next(
        r for r in workspace.read_records() if r.get("type") == "metric_wait" and r.get("phase") == "terminal"
    )
    assert terminal["write_semantics_verified"] is False
    assert not record_outcomes(plan)[-1]["ok"]
    assert not json.loads(server.record_resolution("Claim actual writes succeeded", True))["ok"]
    assert (
        str(judge_resolution(server._outcome_evidence(workspace.read_records()), agent_succeeded=True).state)
        == "UNRESOLVED"
    )


def test_verified_normal_producer_accounting_resolves_full_public_flow(plan, runtime, metric_data, monkeypatch):
    """Completed counters tied to the restored image prove writes in both bins."""

    def normalize(value):
        if isinstance(value, dict):
            return {("ServiceName" if k == "Service" else k): normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, str):
            if value.startswith("{"):
                return json.dumps(normalize(json.loads(value)))
            return "app" if value == "observed-api" else "ServiceName" if value == "Service" else value
        return value

    observed = normalize(metric_data)
    descriptor = accounting_context(plan)
    descriptor.update(namespace="Product/Requests", attempts_metric="WriteCompletions", failures_metric="WriteErrors")
    observed["context"]["playbook"]["execution_steps"][2]["metric_wait"]["completed_work_evidence"] = {
        "record_index": "approved_context",
        "json_pointer": "/playbook/rollback_context/write_accounting",
    }
    run_full_tools(plan, runtime, observed, monkeypatch)
    workspace, _ = runtime
    assert all(r["ok"] for r in record_outcomes(plan))
    assert json.loads(server.record_resolution("Both fixed bins prove committed writes", True))["ok"]
    assert (
        str(judge_resolution(server._outcome_evidence(workspace.read_records()), agent_succeeded=True).state)
        == "RESOLVED"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "",
        "old_time",
        "foreign_task",
        "read_event",
        "zero_count",
        "bool_count",
        "truncated",
        "foreign_execution",
        "wrong_group",
        "model_text",
        "query",
    ],
)
def test_actual_approved_committed_log_required_when_accounting_unknown(
    plan, runtime, metric_data, monkeypatch, mutation
):
    """Only a restored task's committed event can supplement arithmetic metrics."""
    run_full_tools(plan, runtime, metric_data, monkeypatch)
    workspace, _ = runtime
    command = "aws logs filter-log-events --log-group-name /ecs/app --region us-east-1"
    if mutation == "wrong_group":
        command = command.replace("/ecs/app", "/ecs/foreign")
    if mutation == "query":
        command += " --query events"
    plan["execution_steps"].append(
        {
            "step_id": "write-proof",
            "action": "Read committed writes",
            "success_criteria": "Restored task committed rows in fixed window",
            "commands": [command],
        }
    )
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="observed-errors")
    moment = "2026-09-10T12:35:30Z" if mutation != "old_time" else "2026-09-10T12:33:30Z"
    message = {"event": "write_completed", "observed_at": moment, "completion_semantics": "committed_rows", "count": 4}
    if mutation == "read_event":
        message["event"] = "query_completed"
    if mutation == "zero_count":
        message["count"] = 0
    if mutation == "bool_count":
        message["count"] = True
    event = {
        "timestamp": timestamp(moment) * 1000,
        "logStreamName": "ecs/app/" + NEW_TASK.rsplit("/", 1)[-1],
        "message": json.dumps(message),
    }
    if mutation == "foreign_task":
        event["logStreamName"] = "ecs/app/foreign"
    stdout = (
        json.dumps({"events": [event]}) if mutation != "model_text" else json.dumps({"observation": "writes succeeded"})
    )
    monkeypatch.setattr(
        server, "_run_observation_process", lambda argv, budget: subprocess.CompletedProcess(argv, 0, stdout, "")
    )
    monkeypatch.setattr(
        server.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout, "")
    )
    assert json.loads(server.run_playbook_command("write-proof", command))["ok"]
    if mutation in {"truncated", "foreign_execution"}:
        records = workspace.read_records()
        attempt = next(r for r in reversed(records) if r.get("type") == "attempt" and r.get("step_id") == "write-proof")
        attempt["stdout_truncated" if mutation == "truncated" else "execution_id"] = (
            True if mutation == "truncated" else "other"
        )
        assert server._outcome_evidence(records).step("verify").contract_error
        return
    results = record_outcomes(plan)
    resolution = json.loads(server.record_resolution("Observed committed write log", True))
    if mutation:
        assert not results[3]["ok"]
        assert not resolution["ok"]
    else:
        assert all(r["ok"] for r in results)
        assert resolution["ok"]
