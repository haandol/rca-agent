import json
import subprocess
import tomllib
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from headless_codex.adapters.secondary.codex import codex_execution_runner
from headless_codex.adapters.secondary.codex.codex_execution_runner import (
    CodexExecutionRunner,
)
from headless_codex.services import execution_workspace
from headless_codex.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    APPROVED_SUCCESS_CRITERIA_ENV,
    EXECUTION_DEADLINE_ENV,
    ExecutionWorkspace,
)


@pytest.fixture(autouse=True)
def isolated_codex_runtime_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_RUNTIME_HOME_ROOT", str(tmp_path / "codex-runs"))


class _Process:
    returncode = 0

    def __init__(self, args):
        self.args = args

    def communicate(self, input: str, timeout: int):
        Path(self.args[self.args.index("-o") + 1]).write_text("done")
        return "", ""


def _prepared_workspace(monkeypatch, tmp_path) -> ExecutionWorkspace:
    monkeypatch.setattr(execution_workspace, "_WORKSPACE_ROOT", tmp_path / "executions")
    workspace = ExecutionWorkspace.create("exec-1")
    workspace.prepare()
    return workspace


def test_execution_runner_passes_only_the_approved_contract(monkeypatch, tmp_path):
    captured = {}
    workspace = _prepared_workspace(monkeypatch, tmp_path)

    def _popen(args, **kwargs):
        captured.update(kwargs)
        captured["config"] = tomllib.loads((Path(kwargs["env"]["CODEX_HOME"]) / "config.toml").read_text())
        return _Process(args)

    monkeypatch.setattr(codex_execution_runner.subprocess, "Popen", _popen)

    result = CodexExecutionRunner().run_execution(
        "run",
        execution_token=workspace.token,
        execution_id="exec-1",
        approved_step_ids=("step-1", "step-2"),
        approved_success_criteria={"step-1": "healthy", "step-2": "no errors"},
        cancel_checker=lambda: False,
    )

    assert result.success
    assert json.loads(captured["env"][APPROVED_STEP_IDS_ENV]) == ["step-1", "step-2"]
    assert json.loads(captured["env"][APPROVED_SUCCESS_CRITERIA_ENV]) == {
        "step-1": "healthy",
        "step-2": "no errors",
    }
    assert captured["config"]["model"] == "global.openai.gpt-5.6-sol"
    assert captured["config"]["model_reasoning_effort"] == "high"


def test_execution_and_retrospective_use_separate_role_configs(monkeypatch, tmp_path):
    calls = []
    workspace = _prepared_workspace(monkeypatch, tmp_path)

    def _popen(args, **kwargs):
        home = Path(kwargs["env"]["CODEX_HOME"])
        calls.append(
            {
                "config": tomllib.loads((home / "config.toml").read_text()),
                "agents": {
                    path.name: tomllib.loads(path.read_text()) for path in sorted((home / "agents").glob("*.toml"))
                },
            }
        )
        return _Process(args)

    monkeypatch.setattr(codex_execution_runner.subprocess, "Popen", _popen)
    runner = CodexExecutionRunner()
    runner.run_execution(
        "run",
        execution_token=workspace.token,
        execution_id="exec-1",
        approved_step_ids=("step-1",),
        approved_success_criteria={"step-1": "healthy"},
        cancel_checker=lambda: False,
    )
    runner.run_retrospective("review", execution_token=workspace.token, execution_id="exec-1")

    execution_servers = calls[0]["config"]["mcp_servers"]
    retrospective_servers = calls[1]["config"]["mcp_servers"]
    assert set(execution_servers) == {"playbook-execution"}
    assert execution_servers["playbook-execution"]["enabled_tools"] == [
        "run_playbook_command",
        "wait_for_post_action_metrics",
        "record_step_outcome",
        "record_resolution",
    ]
    assert set(retrospective_servers) == {"playbook-retrospective"}
    assert retrospective_servers["playbook-retrospective"]["enabled_tools"] == ["save_playbook_update"]


class _Clock:
    epoch = 1000.0
    monotonic = 50.0

    def advance(self, seconds):
        self.epoch += seconds
        self.monotonic += seconds


def _control(monkeypatch, tmp_path, *, timeout=30):
    workspace = _prepared_workspace(monkeypatch, tmp_path)
    clock = _Clock()
    monkeypatch.setattr(codex_execution_runner.time, "time", lambda: clock.epoch)
    monkeypatch.setattr(codex_execution_runner.time, "monotonic", lambda: clock.monotonic)
    control = codex_execution_runner._ObservationControl(
        workspace.token, workspace.execution_id, clock.epoch + timeout, clock.monotonic + timeout
    )
    return control, clock


def _read_control(control):
    return json.loads(control.path.read_text())


def test_control_refresh_keeps_fixed_deadline_and_token_scoped_identity(monkeypatch, tmp_path):
    control, clock = _control(monkeypatch, tmp_path)
    assert _read_control(control)["active"] is False
    for _ in range(4):
        assert control.refresh(lambda: False)
        record = _read_control(control)
        assert record == {
            "execution_id": "exec-1",
            "deadline_epoch": 1030,
            "checked_at_epoch": clock.epoch,
            "active": True,
        }
        assert control.path.stat().st_mode & 0o777 == 0o600
        clock.advance(5)
    assert control.remaining() == 10
    control.deactivate()
    assert not control.refresh(lambda: False)
    assert _read_control(control)["active"] is False


@pytest.mark.parametrize("cause", ["cancel", "error", "missing_checker", "deadline", "slow_check"])
def test_control_fails_closed_and_cannot_be_reactivated(monkeypatch, tmp_path, cause):
    control, clock = _control(monkeypatch, tmp_path)
    assert control.refresh(lambda: False)
    checker = Mock(return_value=False)
    if cause == "cancel":
        checker.return_value = True
    elif cause == "error":
        checker = Mock(side_effect=RuntimeError("store unavailable"))
    elif cause == "missing_checker":
        checker = None
    elif cause == "deadline":
        clock.advance(30)
    elif cause == "slow_check":
        checker.side_effect = lambda: clock.advance(5)

    assert not control.refresh(checker)
    assert _read_control(control)["active"] is False
    assert not control.refresh(lambda: False)
    assert _read_control(control)["deadline_epoch"] == 1030


def test_late_claim_check_cannot_reactivate_after_runner_finally(monkeypatch, tmp_path):
    control, _ = _control(monkeypatch, tmp_path)
    entered, finish = Event(), Event()

    def checker():
        entered.set()
        assert finish.wait(2)
        return False

    thread = Thread(target=lambda: control.refresh(checker))
    thread.start()
    try:
        assert entered.wait(2)
        control.deactivate()
        assert _read_control(control)["active"] is False
    finally:
        finish.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert _read_control(control)["active"] is False


@pytest.mark.parametrize("cause", ["cancel", "error", "deadline"])
def test_watcher_publishes_inactive_before_termination(monkeypatch, tmp_path, cause):
    control, clock = _control(monkeypatch, tmp_path, timeout=7)
    assert control.refresh(lambda: False)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock.advance(seconds)
        return False

    monkeypatch.setattr(control.stop_event, "wait", wait)
    checker = (lambda: True) if cause == "cancel" else (lambda: False)
    if cause == "error":
        checker = Mock(side_effect=RuntimeError("store unavailable"))
    proc = Mock()
    proc.terminate.side_effect = lambda: (
        pytest.fail("termination preceded control stop") if _read_control(control)["active"] else None
    )
    codex_execution_runner._watch_cancel(proc, control, checker)
    assert waits == ([5, 2] if cause == "deadline" else [5])
    proc.terminate.assert_called_once()
    assert proc.wait.call_args.kwargs["timeout"] <= control.remaining()
    assert _read_control(control)["deadline_epoch"] == 1007


@pytest.mark.parametrize("end", ["success", "timeout", "missing_cli", "exception"])
def test_runner_fixed_budget_env_and_final_inactive(monkeypatch, tmp_path, end):
    workspace = _prepared_workspace(monkeypatch, tmp_path)
    clock = _Clock()
    monkeypatch.setattr(codex_execution_runner.time, "time", lambda: clock.epoch)
    monkeypatch.setattr(codex_execution_runner.time, "monotonic", lambda: clock.monotonic)
    monkeypatch.setattr(codex_execution_runner, "EXECUTION_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(codex_execution_runner, "Thread", Mock())
    prepare = codex_execution_runner.prepare_workspace

    def slow_setup(path, profile):
        clock.advance(4)
        return prepare(path, profile)

    monkeypatch.setattr(codex_execution_runner, "prepare_workspace", slow_setup)
    control_path = execution_workspace.observation_control_path_for_token(workspace.token)
    captured = {}

    def _popen(args, **kwargs):
        captured.update(kwargs)
        assert json.loads(control_path.read_text())["active"] is True
        assert kwargs["env"][EXECUTION_DEADLINE_ENV] == "1030.0"
        if end == "missing_cli":
            raise FileNotFoundError()
        clock.advance(3)
        proc = Mock(returncode=0)

        def communicate(**kwargs):
            assert kwargs["timeout"] == 23
            if end == "exception":
                raise RuntimeError("unexpected failure")
            if end == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return "", ""

        proc.communicate.side_effect = communicate
        proc.kill.side_effect = lambda: (
            pytest.fail("timeout kill preceded control stop")
            if json.loads(control_path.read_text())["active"]
            else None
        )
        return proc

    monkeypatch.setattr(codex_execution_runner.subprocess, "Popen", _popen)
    kwargs = {
        "execution_token": workspace.token,
        "execution_id": "exec-1",
        "approved_step_ids": ("step-1",),
        "approved_success_criteria": {"step-1": "healthy"},
        "cancel_checker": lambda: False,
    }
    if end == "exception":
        with pytest.raises(RuntimeError):
            CodexExecutionRunner().run_execution("run", **kwargs)
    else:
        result = CodexExecutionRunner().run_execution("run", **kwargs)
        assert result.success is (end == "success")
    assert json.loads(control_path.read_text()) == {
        "execution_id": "exec-1",
        "deadline_epoch": 1030,
        "checked_at_epoch": clock.epoch,
        "active": False,
    }


@pytest.mark.parametrize("checker", [None, lambda: True, Mock(side_effect=RuntimeError("unavailable"))])
def test_runner_never_launches_without_current_claim(monkeypatch, tmp_path, checker):
    workspace = _prepared_workspace(monkeypatch, tmp_path)
    popen = Mock()
    monkeypatch.setattr(codex_execution_runner.subprocess, "Popen", popen)
    result = CodexExecutionRunner().run_execution(
        "run",
        execution_token=workspace.token,
        execution_id="exec-1",
        approved_step_ids=("step-1",),
        approved_success_criteria={"step-1": "healthy"},
        cancel_checker=checker,
    )
    assert not result.success
    assert result.cancelled
    popen.assert_not_called()
    record = json.loads(execution_workspace.observation_control_path_for_token(workspace.token).read_text())
    assert record["active"] is False


@pytest.mark.parametrize("cancelled", [False, True])
def test_retrospective_callback_after_resolved_is_independent_of_execution_control(monkeypatch, tmp_path, cancelled):
    workspace = _prepared_workspace(monkeypatch, tmp_path)
    control_path = execution_workspace.observation_control_path_for_token(workspace.token)
    execution_workspace.write_observation_json(
        control_path, {"execution_id": "exec-1", "active": False, "deadline_epoch": 0, "checked_at_epoch": 0}
    )  # the originating execution is already RESOLVED and its observation control is inactive
    prior_execution_process = Mock()
    retrospective_process = Mock(returncode=0)
    watcher_calls = []

    def terminate():
        retrospective_process.returncode = -15

    retrospective_process.terminate.side_effect = terminate
    retrospective_process.communicate.return_value = ("done", "")
    monkeypatch.setattr(codex_execution_runner.subprocess, "Popen", Mock(return_value=retrospective_process))
    monkeypatch.setattr(
        codex_execution_runner, "_ObservationControl", Mock(side_effect=AssertionError("execution-only"))
    )

    class ImmediateWatcher:
        def __init__(self, *, target, args, daemon):
            self.target, self.args = target, args
            watcher_calls.append(target)

        def start(self):
            stop_event = self.args[1]
            # Run exactly one callback check synchronously, then stop the test watcher.
            monkeypatch.setattr(stop_event, "wait", Mock(side_effect=[False, True]))
            self.target(*self.args)

    monkeypatch.setattr(codex_execution_runner, "Thread", ImmediateWatcher)
    callback = Mock(return_value=cancelled)
    result = CodexExecutionRunner().run_retrospective(
        "review", execution_token=workspace.token, execution_id="exec-1", cancel_checker=callback
    )
    assert watcher_calls == [codex_execution_runner._watch_retrospective_cancel]
    callback.assert_called_once()
    assert result.cancelled is cancelled and result.success is not cancelled
    if cancelled:
        retrospective_process.terminate.assert_called_once()
    else:
        retrospective_process.terminate.assert_not_called()
    prior_execution_process.terminate.assert_not_called()
    assert json.loads(control_path.read_text())["active"] is False


def test_control_write_and_unlink_failure_cannot_refresh_or_mask_outcome(monkeypatch, tmp_path):
    control, clock = _control(monkeypatch, tmp_path)
    assert control.refresh(lambda: False)
    previous = _read_control(control)
    write = Mock(side_effect=OSError("write failed"))
    unlink = Mock(side_effect=OSError("unlink also failed"))
    monkeypatch.setattr(codex_execution_runner, "write_observation_json", write)
    monkeypatch.setattr(Path, "unlink", unlink)
    clock.advance(5)
    assert not control.refresh(lambda: False)
    assert control.stop_event.is_set()
    control.deactivate()  # cleanup cannot raise and mask an existing success/failure
    assert _read_control(control) == previous  # no falsely fresh active permission
    clock.advance(6)
    assert clock.epoch - previous["checked_at_epoch"] > 10  # reader rejects stale heartbeat
    assert write.call_count == 2 and unlink.call_count == 2
