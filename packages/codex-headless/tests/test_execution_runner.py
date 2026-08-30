import json
import tomllib
from pathlib import Path

import pytest

from codex_headless.adapters.secondary.codex import codex_execution_runner
from codex_headless.adapters.secondary.codex.codex_execution_runner import (
    CodexExecutionRunner,
)
from codex_headless.services import execution_workspace
from codex_headless.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    APPROVED_SUCCESS_CRITERIA_ENV,
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
    )
    runner.run_retrospective("review", execution_token=workspace.token, execution_id="exec-1")

    execution_servers = calls[0]["config"]["mcp_servers"]
    retrospective_servers = calls[1]["config"]["mcp_servers"]
    assert set(execution_servers) == {"cloudwatch", "playbook-execution"}
    assert execution_servers["playbook-execution"]["enabled_tools"] == [
        "run_playbook_command",
        "record_step_outcome",
        "record_resolution",
    ]
    assert set(retrospective_servers) == {"playbook-retrospective"}
    assert retrospective_servers["playbook-retrospective"]["enabled_tools"] == ["save_playbook_update"]
