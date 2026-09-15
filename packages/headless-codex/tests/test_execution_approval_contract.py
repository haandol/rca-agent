"""Approval boundary tests exercise the public MCP calls against an immutable snapshot."""

import copy
import fcntl
import json
import subprocess
import time
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from headless_codex import execution_mcp_server as server
from headless_codex.services import execution_workspace as wm
from headless_codex.services.execution_contract import command_digest, validate_steps
from headless_codex.services.execution_outcome import assemble_evidence, judge_resolution
from headless_codex.services.execution_state import ExecutionState

READ = "aws ecs describe-tasks --cluster demo --tasks owner --region us-east-1"
WRITE = "aws ecs stop-task --cluster demo --task owner --region us-east-1"
VERIFY = "aws cloudwatch describe-alarms --alarm-names recovery --region us-east-1"


@pytest.fixture
def approved(monkeypatch, tmp_path):
    """Install the worker-owned snapshot once; test calls cannot change its approval."""
    monkeypatch.setattr(wm, "_WORKSPACE_ROOT", tmp_path)
    workspace = wm.ExecutionWorkspace.create("exec-approval")
    workspace.prepare()
    monkeypatch.setenv(wm.EXECUTION_TOKEN_ENV, workspace.token)
    monkeypatch.setenv(wm.EXECUTION_ID_ENV, workspace.execution_id)
    plan = {
        "execution_steps": [
            {"step_id": "action", "commands": [READ, WRITE], "metric_wait": None, "success_criteria": "owner stopped"},
            {"step_id": "verify", "commands": [VERIFY], "success_criteria": "alarm OK"},
        ]
    }
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="recovery")
    wm.write_observation_json(
        wm.observation_control_path_for_token(workspace.token),
        {
            "execution_id": workspace.execution_id,
            "active": True,
            "checked_at_epoch": time.time(),
            "deadline_epoch": time.time() + 1000,
        },
    )
    process = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(server.subprocess, "run", process)
    return workspace, plan, process


def run(step, command):
    return json.loads(server.run_playbook_command(step, command))


def outcome(step, criterion):
    return json.loads(server.record_step_outcome(step, criterion, "Observed actual criterion", True))


def resolved(workspace, plan):
    evidence = assemble_evidence(
        workspace.read_records(),
        execution_id=workspace.execution_id,
        rca_id="rca",
        engine="strands",
        playbook=plan,
    )
    return judge_resolution(evidence, agent_succeeded=True).state


def test_strands_null_wait_runs_every_exact_command_in_order_and_resolves(approved):
    workspace, plan, process = approved
    snapshot = copy.deepcopy(plan)
    assert run("action", READ)["ok"]
    assert not outcome("action", "owner stopped")["ok"]
    assert run("action", WRITE)["ok"]
    assert outcome("action", "owner stopped")["ok"]
    assert run("verify", VERIFY)["ok"]
    assert outcome("verify", "alarm OK")["ok"]
    assert json.loads(server.record_resolution("Recovery observed", True))["ok"]
    assert resolved(workspace, plan) is ExecutionState.RESOLVED
    assert plan == snapshot
    assert [call.args[0] for call in process.call_args_list] == [READ.split(), WRITE.split(), VERIFY.split()]
    assert [r["command_index"] for r in workspace.read_records() if r["type"] == "attempt"] == [0, 1, 0]


@pytest.mark.parametrize(
    "changed",
    [
        WRITE.replace("owner", "other"),
        WRITE.replace("us-east-1", "us-west-2"),
        WRITE + " --reason extra",
        WRITE.replace("aws ecs", "aws  ecs"),
        "aws cloudwatch list-metrics --region us-east-1",
    ],
)
def test_any_unapproved_command_or_target_change_is_never_spawned_and_cannot_be_erased(approved, changed):
    workspace, plan, process = approved
    assert not run("action", changed)["ok"]
    process.assert_not_called()
    assert run("action", READ)["ok"]
    assert run("action", WRITE)["ok"]
    assert not outcome("action", "owner stopped")["ok"]
    assert resolved(workspace, plan) is ExecutionState.UNRESOLVED
    assert workspace.read_records()[0]["type"] == "approval_rejection"


@pytest.mark.parametrize("step,command", [("action", WRITE), ("verify", VERIFY)])
def test_a_command_or_step_cannot_skip_its_approved_predecessor(approved, step, command):
    _, _, process = approved
    assert not run(step, command)["ok"]
    process.assert_not_called()


def test_a_successful_write_is_never_repeated_even_before_the_next_step(approved):
    _, _, process = approved
    assert run("action", READ)["ok"]
    assert run("action", WRITE)["ok"]
    repeated = run("action", WRITE)
    assert not repeated["ok"] and "successful write" in repeated["error"]
    assert process.call_count == 2


def test_an_interrupted_write_with_unknown_result_cannot_be_retried(approved):
    workspace, _, process = approved
    assert run("action", READ)["ok"]
    process.side_effect = RuntimeError("MCP process interrupted during command")
    with pytest.raises(RuntimeError, match="interrupted"):
        run("action", WRITE)
    assert workspace.read_records()[-1]["type"] == "command_started"
    process.side_effect = None
    assert "unknown result" in run("action", WRITE)["error"]
    assert process.call_count == 2


def test_concurrent_command_call_cannot_pass_the_same_write_position(approved):
    workspace, _, process = approved
    with (workspace.path / "execution-operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "already running" in run("action", READ)["error"]
    process.assert_not_called()
    assert run("action", READ)["ok"]


def test_identical_transient_failure_retry_can_complete_the_approved_position(approved):
    workspace, plan, process = approved
    assert run("action", READ)["ok"]
    process.return_value = subprocess.CompletedProcess([], 254, "", "ThrottlingException")
    assert not run("action", WRITE)["ok"]
    process.return_value = subprocess.CompletedProcess([], 0, "{}", "")
    assert run("action", WRITE)["ok"]
    assert outcome("action", "owner stopped")["ok"]
    assert run("verify", VERIFY)["ok"]
    assert outcome("verify", "alarm OK")["ok"]
    assert json.loads(server.record_resolution("Recovery observed", True))["ok"]
    assert resolved(workspace, plan) is ExecutionState.RESOLVED


def test_approved_reads_can_repeat_until_execution_advances(approved):
    _, _, process = approved
    assert run("action", READ)["ok"]
    assert run("action", READ)["ok"]
    assert run("action", WRITE)["ok"]
    assert run("verify", VERIFY)["ok"]
    assert "advanced" in run("action", READ)["error"]
    assert process.call_count == 4


def test_failure_of_one_mandatory_command_cannot_be_hidden_by_another_success(approved):
    workspace, plan, process = approved
    process.return_value = subprocess.CompletedProcess([], 254, "", "AccessDenied")
    assert not run("action", READ)["ok"]
    process.return_value = subprocess.CompletedProcess([], 0, "{}", "")
    assert not run("action", WRITE)["ok"]
    assert not outcome("action", "owner stopped")["ok"]
    assert not run("verify", VERIFY)["ok"]
    assert not outcome("verify", "alarm OK")["ok"]
    assert process.call_count == 1
    assert not json.loads(server.record_resolution("Recovery observed", True))["ok"]
    assert resolved(workspace, plan) is ExecutionState.UNRESOLVED


def test_failed_preceding_step_cannot_authorize_a_later_write(approved):
    workspace, plan, process = approved
    plan["execution_steps"][0]["commands"] = [READ]
    plan["execution_steps"][1]["commands"] = [WRITE]
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="recovery")
    process.return_value = subprocess.CompletedProcess([], 254, "", "AccessDenied")
    assert not run("action", READ)["ok"]
    process.return_value = subprocess.CompletedProcess([], 0, "{}", "")
    assert not run("verify", WRITE)["ok"]
    assert process.call_count == 1


@pytest.mark.parametrize(
    "diagnostic", [VERIFY, "aws logs filter-log-events --log-group-name /owner --region us-east-1"]
)
def test_failed_action_still_allows_approved_diagnostic_reads(approved, diagnostic):
    workspace, plan, process = approved
    plan["execution_steps"][0]["commands"] = [WRITE]
    plan["execution_steps"][1]["commands"] = [diagnostic]
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="recovery")
    process.return_value = subprocess.CompletedProcess([], 254, "", "AccessDenied")
    assert not run("action", WRITE)["ok"]
    process.return_value = subprocess.CompletedProcess([], 0, "{}", "")
    assert run("verify", diagnostic)["ok"]
    assert run("verify", diagnostic)["ok"]
    assert outcome("verify", "alarm OK")["ok"]
    assert resolved(workspace, plan) is ExecutionState.UNRESOLVED
    assert process.call_count == 3


def test_transient_prerequisite_retry_then_next_command_succeeds(approved):
    _, _, process = approved
    process.return_value = subprocess.CompletedProcess([], 254, "", "ThrottlingException")
    assert not run("action", READ)["ok"]
    process.return_value = subprocess.CompletedProcess([], 0, "{}", "")
    assert run("action", READ)["ok"]
    assert run("action", WRITE)["ok"]
    assert process.call_count == 3


def test_policy_block_preserves_manual_step_and_allows_remaining_approved_steps(approved):
    workspace, plan, process = approved
    blocked = "aws ecs delete-service --cluster demo --service api"
    plan["execution_steps"][0]["commands"] = [blocked]
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="recovery")
    assert run("action", blocked)["blocked"]
    assert json.loads(
        server.record_step_outcome("action", "owner stopped", "Manual action", False, manual_action_required=True)
    )["ok"]
    assert run("verify", VERIFY)["ok"]
    assert outcome("verify", "alarm OK")["ok"]
    assert resolved(workspace, plan) is ExecutionState.UNRESOLVED
    assert process.call_count == 1


def test_latest_failed_read_cannot_reuse_an_earlier_success_for_resolution(approved):
    workspace, plan, process = approved
    assert run("action", READ)["ok"]
    assert run("action", WRITE)["ok"]
    assert outcome("action", "owner stopped")["ok"]
    assert run("verify", VERIFY)["ok"]
    assert outcome("verify", "alarm OK")["ok"]
    assert json.loads(server.record_resolution("Recovery observed", True))["ok"]
    process.return_value = subprocess.CompletedProcess([], 254, "", "ThrottlingException")
    assert not run("verify", VERIFY)["ok"]
    assert not outcome("verify", "alarm OK")["ok"]
    assert not json.loads(server.record_resolution("Recovery observed", True))["ok"]
    assert resolved(workspace, plan) is ExecutionState.UNRESOLVED
    process.return_value = subprocess.CompletedProcess([], 0, "{}", "")
    assert run("verify", VERIFY)["ok"]
    assert outcome("verify", "alarm OK")["ok"]
    assert json.loads(server.record_resolution("Recovery reobserved", True))["ok"]
    assert resolved(workspace, plan) is ExecutionState.RESOLVED


def test_inflight_read_cannot_reuse_an_earlier_success_for_resolution(approved):
    workspace, plan, process = approved
    assert run("action", READ)["ok"]
    assert run("action", WRITE)["ok"]
    assert outcome("action", "owner stopped")["ok"]
    assert run("verify", VERIFY)["ok"]
    assert outcome("verify", "alarm OK")["ok"]
    assert json.loads(server.record_resolution("Recovery observed", True))["ok"]
    entered, release = Event(), Event()

    def pending(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return subprocess.CompletedProcess([], 0, "{}", "")

    process.side_effect = pending
    worker = Thread(target=lambda: run("verify", VERIFY))
    worker.start()
    try:
        assert entered.wait(5)
        assert not outcome("verify", "alarm OK")["ok"]
        assert not json.loads(server.record_resolution("Recovery observed", True))["ok"]
        assert resolved(workspace, plan) is ExecutionState.UNRESOLVED
        assert "already running" in run("verify", VERIFY)["error"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert process.call_count == 4


@pytest.mark.parametrize("control_state", ["missing", "inactive", "stale", "expired", "wrong_execution"])
def test_command_cannot_start_without_current_execution_control(approved, control_state):
    workspace, plan, process = approved
    plan["execution_steps"][0]["commands"] = [WRITE]
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="recovery")
    path = wm.observation_control_path_for_token(workspace.token)
    control = json.loads(path.read_text())
    if control_state == "missing":
        path.unlink()
    else:
        if control_state == "inactive":
            control["active"] = False
        elif control_state == "stale":
            control["checked_at_epoch"] = time.time() - 11
        elif control_state == "expired":
            control["deadline_epoch"] = time.time() - 1
        else:
            control["execution_id"] = "another-execution"
        wm.write_observation_json(path, control)
    assert not run("action", WRITE)["ok"]
    process.assert_not_called()
    assert not any(r["type"] == "command_started" for r in workspace.read_records())


def test_command_timeout_cannot_exceed_remaining_execution_budget(approved):
    workspace, _, process = approved
    path = wm.observation_control_path_for_token(workspace.token)
    control = json.loads(path.read_text())
    control["deadline_epoch"] = time.time() + 7
    wm.write_observation_json(path, control)
    assert run("action", READ)["ok"]
    assert 0 < process.call_args.kwargs["timeout"] <= 7


def test_final_replay_rejects_missing_identity_even_with_successful_model_outcomes(approved):
    workspace, plan, _ = approved
    for step, command in [("action", READ), ("action", WRITE), ("verify", VERIFY)]:
        server._append_record(
            {"type": "attempt", "step_id": step, "command": command, "succeeded": True, "exit_status": "0"}
        )
    server._append_record({"type": "resolution", "resolved": True, "observation": "claimed"})
    assert resolved(workspace, plan) is ExecutionState.UNRESOLVED


def test_final_replay_rejects_successful_write_duplication_at_a_second_approved_position(approved):
    workspace, plan, _ = approved
    plan["execution_steps"][0]["commands"] = [WRITE, WRITE]
    for index in (0, 1):
        server._append_record(
            {
                "type": "attempt",
                "step_id": "action",
                "command": WRITE,
                "command_digest": command_digest(WRITE),
                "command_index": index,
                "succeeded": True,
                "exit_status": "0",
            }
        )
    assert resolved(workspace, plan) is ExecutionState.UNRESOLVED


@pytest.mark.parametrize(
    "operation", [{}, {"commands": []}, {"metric_wait": None}, {"commands": [], "metric_wait": None}]
)
def test_legacy_or_empty_operations_are_rejected_before_any_command(approved, operation):
    workspace, _, process = approved
    plan = {"execution_steps": [{"step_id": "action", "success_criteria": "healthy", **operation}]}
    workspace.write_observation_context(playbook=plan, alarm_data={}, alarm_name="recovery")
    assert not run("action", READ)["ok"]
    process.assert_not_called()
    assert resolved(workspace, plan) is ExecutionState.UNRESOLVED


def test_normalization_drops_only_null_inactive_fields_without_mutating_the_snapshot():
    metric = {"namespace": "App", "dimensions": {"Service": "api"}, "metric_name": "Attempts"}
    wait = {
        "action_step_id": "action",
        "metrics": {"attempts": metric, "failures": {**metric, "metric_name": "Failures"}},
        "failure_alarm_name": "failure",
        "region": "us-east-1",
    }
    plan = {
        "execution_steps": [
            {"step_id": "action", "commands": [WRITE], "metric_wait": None, "success_criteria": "stopped"},
            {"step_id": "verify", "commands": [], "metric_wait": wait, "success_criteria": "Failures failure OK"},
        ]
    }
    snapshot = copy.deepcopy(plan)
    steps = validate_steps(plan)
    assert "metric_wait" not in steps[0]
    assert "commands" not in steps[1]
    assert steps[1]["metric_wait"] == wait
    assert plan == snapshot
