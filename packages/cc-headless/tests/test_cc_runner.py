import json
import os
import subprocess
from pathlib import Path

from cc_headless.adapters.secondary.cc import cc_subprocess_runner
from cc_headless.adapters.secondary.cc.cc_subprocess_runner import CcSubprocessRunner

EXECUTION_TOKEN = "a" * 32


class FakeProcess:
    def __init__(self, stdout: str = '{"result": "analysis complete"}', stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.killed = False

    def communicate(self, timeout: int) -> tuple[str, str]:
        self.timeout = timeout
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: int | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15


def _capture_processes(monkeypatch, processes: list[FakeProcess] | None = None) -> list[dict]:
    calls: list[dict] = []
    queued = list(processes or [])

    def _popen(args, **kwargs):
        calls.append({"args": args, **kwargs})
        return queued.pop(0) if queued else FakeProcess()

    monkeypatch.setattr(cc_subprocess_runner.subprocess, "Popen", _popen)
    return calls


def test_runner_invokes_headless_json_mode_with_custom_mcp_config(monkeypatch, tmp_path):
    calls = _capture_processes(monkeypatch)
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text("{}")

    result = CcSubprocessRunner().run(
        "investigate",
        execution_token=EXECUTION_TOKEN,
        mcp_config=str(mcp_config),
    )

    assert result.success is True
    assert result.result == "analysis complete"
    assert calls[0]["args"][:3] == ["claude", "-p", "investigate"]
    assert "--output-format" in calls[0]["args"]
    assert calls[0]["args"][calls[0]["args"].index("--output-format") + 1] == "json"
    assert calls[0]["args"][calls[0]["args"].index("--mcp-config") + 1] == str(mcp_config)


def test_runner_uses_strict_mcp_config_to_exclude_user_servers(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert "--strict-mcp-config" in calls[0]["args"]


def test_runner_does_not_resume_or_continue_prior_conversations(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CcSubprocessRunner().run("first", execution_token=EXECUTION_TOKEN)
    CcSubprocessRunner().run("second", execution_token=EXECUTION_TOKEN)

    for call in calls:
        assert "--resume" not in call["args"]
        assert "--continue" not in call["args"]
        assert "-c" not in call["args"]


def test_runner_isolates_working_and_home_directories_per_run(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CcSubprocessRunner().run("first", execution_token=EXECUTION_TOKEN)
    CcSubprocessRunner().run("second", execution_token=EXECUTION_TOKEN)

    first, second = calls
    assert first["cwd"] != second["cwd"]
    assert first["env"]["HOME"] != second["env"]["HOME"]
    assert Path(first["cwd"]).is_absolute()
    assert Path(second["cwd"]).is_absolute()
    assert Path(first["env"]["HOME"]).is_absolute()
    assert Path(second["env"]["HOME"]).is_absolute()


def test_runner_preserves_parent_environment_without_mutating_home(monkeypatch):
    calls = _capture_processes(monkeypatch)
    monkeypatch.setenv("RCA_TEST_SENTINEL", "preserved")
    parent_home = os.environ.get("HOME")

    CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert calls[0]["env"]["RCA_TEST_SENTINEL"] == "preserved"
    assert calls[0]["env"]["HOME"] != parent_home
    assert os.environ.get("HOME") == parent_home


def test_runner_default_mcp_config_is_an_existing_absolute_file(monkeypatch):
    configs = []

    def _popen(args, **kwargs):
        index = args.index("--mcp-config") + 1
        configs.append(json.loads(Path(args[index]).read_text()))
        return FakeProcess()

    monkeypatch.setattr(cc_subprocess_runner.subprocess, "Popen", _popen)

    CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert configs[0]["mcpServers"]["rca-progress"]


def test_runner_resolves_packaged_mcp_server_against_the_installed_package(monkeypatch):
    """The committed placeholder must become a real path that exists in this environment."""
    servers = []

    def _popen(args, **kwargs):
        index = args.index("--mcp-config") + 1
        rendered = json.loads(Path(args[index]).read_text())
        servers.append(rendered["mcpServers"]["rca-progress"]["args"][1])
        return FakeProcess()

    monkeypatch.setattr(cc_subprocess_runner.subprocess, "Popen", _popen)

    CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    target = servers[0]
    assert "{{PACKAGE_ROOT}}" not in target
    module_path, _, attribute = target.rpartition(":")
    assert attribute == "mcp"
    assert Path(module_path).is_absolute()
    assert Path(module_path).is_file()


def test_runner_does_not_leave_rendered_mcp_config_behind(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    rendered = Path(calls[0]["args"][calls[0]["args"].index("--mcp-config") + 1])
    assert not rendered.exists()


def test_runner_returns_actionable_error_when_cli_is_missing(monkeypatch):
    def _missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(cc_subprocess_runner.subprocess, "Popen", _missing)

    result = CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert result.success is False
    assert "Claude Code CLI not found" in result.result
    assert result.raw_output == ""


def test_runner_preserves_non_json_stdout(monkeypatch):
    _capture_processes(monkeypatch, [FakeProcess(stdout="plain result\n")])

    result = CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert result.success is True
    assert result.result == "plain result"
    assert result.raw_output == "plain result\n"


def test_runner_kills_process_after_timeout(monkeypatch):
    process = FakeProcess()

    def _timeout(timeout: int):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    process.communicate = _timeout
    _capture_processes(monkeypatch, [process])

    result = CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert result.success is False
    assert "timed out" in result.result
    assert process.killed is True


def test_runner_reports_nonzero_exit_and_retains_diagnostic_output(monkeypatch):
    _capture_processes(monkeypatch, [FakeProcess(stdout="", stderr="provider failed", returncode=2)])

    result = CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert result.success is False
    assert "rc=2" in result.result
    assert result.raw_output == "provider failed"


def test_runner_exposes_only_agent_skill_and_strict_mcp_tools(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    args = calls[0]["args"]
    builtins = set(args[args.index("--tools") + 1].split(","))
    allowed = set(args[args.index("--allowedTools") + 1].split(","))
    dangerous = {"Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebSearch", "WebFetch", "NotebookEdit"}

    assert builtins == {"Agent", "Skill"}
    assert builtins.isdisjoint(dangerous)
    assert allowed == {
        "Agent",
        "Skill",
        "mcp__aws-knowledge__*",
        "mcp__cloudwatch__*",
        "mcp__cloudtrail__*",
        "mcp__github__*",
        "mcp__rca-progress__save_artifact",
    }
    # Analysis is read-only: the only side effect this run may cause is saving an
    # artifact. Recovery lives in a separate agent behind a user approval.
    assert not any("reset" in tool for tool in allowed)
    assert allowed.isdisjoint(dangerous)


def test_runner_uses_restricted_orchestrator_as_the_root_agent(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    args = calls[0]["args"]
    assert args[args.index("--agent") + 1] == "orchestrator"


def test_runner_disables_session_persistence_and_passes_execution_token(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CcSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert "--no-session-persistence" in calls[0]["args"]
    assert calls[0]["env"]["RCA_EXECUTION_TOKEN"] == EXECUTION_TOKEN


def test_runner_passes_claim_context_only_to_the_isolated_child(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CcSubprocessRunner().run(
        "investigate",
        execution_token=EXECUTION_TOKEN,
        rca_id="rca-1",
        claim_token="claim-1",
        attempt=3,
    )

    env = calls[0]["env"]
    assert env["RCA_SESSION_ID"] == "rca-1"
    assert env["RCA_CLAIM_TOKEN"] == "claim-1"
    assert env["RCA_ATTEMPT"] == "3"
