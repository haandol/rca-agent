import json
import os

from cc_headless.adapters.secondary.cc import cc_execution_runner
from cc_headless.adapters.secondary.cc.cc_execution_runner import CcExecutionRunner
from cc_headless.services import execution_workspace
from cc_headless.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    APPROVED_SUCCESS_CRITERIA_ENV,
    ExecutionWorkspace,
)


class _Process:
    returncode = 0

    def communicate(self, timeout):
        return '{"result":"done"}', ""


def test_runner_passes_approved_step_ids_only_to_the_execution_server(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(execution_workspace, "_WORKSPACE_ROOT", tmp_path / "executions")
    workspace = ExecutionWorkspace.create("exec-1")
    workspace.prepare()

    def _popen(args, **kwargs):
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr(cc_execution_runner.subprocess, "Popen", _popen)

    result = CcExecutionRunner().run_execution(
        "run",
        execution_token=workspace.token,
        execution_id="exec-1",
        approved_step_ids=("step-1", "step-2"),
        approved_success_criteria={"step-1": "healthy", "step-2": "no errors"},
    )

    assert result.success
    assert json.loads(captured["env"][APPROVED_STEP_IDS_ENV]) == ["step-1", "step-2"]
    assert json.loads(captured["env"][APPROVED_SUCCESS_CRITERIA_ENV]) == {
        "step-1": "healthy",
        "step-2": "no errors",
    }


def test_runner_overrides_background_wait_ceiling_only_in_child(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "600000")
    monkeypatch.setattr(execution_workspace, "_WORKSPACE_ROOT", tmp_path / "executions")
    workspace = ExecutionWorkspace.create("exec-1")
    workspace.prepare()

    def _popen(args, **kwargs):
        calls.append(kwargs)
        return _Process()

    monkeypatch.setattr(cc_execution_runner.subprocess, "Popen", _popen)
    runner = CcExecutionRunner()

    runner.run_execution(
        "run",
        execution_token=workspace.token,
        execution_id="exec-1",
        approved_step_ids=("step-1",),
        approved_success_criteria={"step-1": "healthy"},
    )
    runner.run_retrospective(
        "review",
        execution_token=workspace.token,
        execution_id="exec-1",
    )

    assert [call["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] for call in calls] == ["0", "0"]
    assert os.environ["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "600000"
