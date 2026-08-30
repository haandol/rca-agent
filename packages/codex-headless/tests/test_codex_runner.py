import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from codex_headless.adapters.secondary.codex import codex_subprocess_runner
from codex_headless.adapters.secondary.codex.codex_harness import MODEL_EVAL_PROFILE
from codex_headless.adapters.secondary.codex.codex_subprocess_runner import (
    CodexSubprocessRunner,
    _last_agent_message,
)

EXECUTION_TOKEN = "a" * 32


@pytest.fixture(autouse=True)
def isolated_codex_runtime_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_RUNTIME_HOME_ROOT", str(tmp_path / "codex-runs"))


class FakeProcess:
    def __init__(
        self,
        args,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        result: str = "analysis complete",
    ):
        self.args = args
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.result = result
        self.killed = False

    def communicate(self, input: str, timeout: int) -> tuple[str, str]:
        self.input = input
        self.timeout = timeout
        if self.returncode == 0:
            Path(self.args[self.args.index("-o") + 1]).write_text(self.result)
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: int | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15


def _capture_processes(monkeypatch, processes: list[dict] | None = None) -> list[dict]:
    calls: list[dict] = []
    queued = list(processes or [])

    def _popen(args, **kwargs):
        home = Path(kwargs["env"]["CODEX_HOME"])
        call = {
            "args": args,
            **kwargs,
            "config": tomllib.loads((home / "config.toml").read_text()),
            "agent_configs": {
                path.name: tomllib.loads(path.read_text()) for path in sorted((home / "agents").glob("*.toml"))
            },
            "guidance": (Path(kwargs["cwd"]) / "AGENTS.md").read_text(),
        }
        calls.append(call)
        return FakeProcess(args, **(queued.pop(0) if queued else {}))

    monkeypatch.setattr(codex_subprocess_runner.subprocess, "Popen", _popen)
    return calls


def test_runner_uses_ephemeral_codex_exec_and_stdin_prompt(monkeypatch):
    calls = _capture_processes(monkeypatch)

    result = CodexSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert result.success is True
    assert result.result == "analysis complete"
    assert calls[0]["args"][:2] == ["codex", "exec"]
    assert "--json" in calls[0]["args"]
    assert "--ephemeral" in calls[0]["args"]
    assert "--strict-config" in calls[0]["args"]
    assert calls[0]["args"][-1] == "-"
    assert "investigate" not in calls[0]["args"]


def test_runner_pins_the_global_profile_high_reasoning_and_task_role_auth(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CodexSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    config = calls[0]["config"]
    assert config["model"] == "global.openai.gpt-5.6-sol"
    assert config["model_provider"] == "amazon-bedrock-runtime"
    assert config["model_reasoning_effort"] == "high"
    provider = config["model_providers"]["amazon-bedrock-runtime"]
    assert provider["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
    assert provider["auth"]["command"] == "python"
    assert provider["auth"]["args"] == ["-m", "codex_headless.bedrock_token"]


def test_analysis_agents_have_disjoint_artifact_writers(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CodexSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert len(calls) == 2
    rca = calls[0]["config"]["mcp_servers"]
    report = calls[1]["config"]["mcp_servers"]
    assert rca["rca-progress"]["enabled_tools"] == ["save_analysis_artifact"]
    assert report["rca-progress"]["enabled_tools"] == ["save_report_artifact"]
    assert all(server["default_tools_approval_mode"] == "approve" for server in rca.values())
    assert report["rca-progress"]["default_tools_approval_mode"] == "approve"
    context_vars = {"RCA_EXECUTION_TOKEN", "RCA_SESSION_ID", "RCA_CLAIM_TOKEN", "RCA_ATTEMPT"}
    assert set(rca["rca-progress"]["env_vars"]) == context_vars
    assert set(report["rca-progress"]["env_vars"]) == context_vars
    ecs_credentials = {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    }
    for server_name in ("aws-knowledge", "cloudwatch", "cloudtrail"):
        assert ecs_credentials.issubset(rca[server_name]["env_vars"])
    assert "playbook-execution" not in rca
    assert "playbook-execution" not in report


def test_model_eval_profile_has_no_live_evidence_servers(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CodexSubprocessRunner().run(
        "evaluate supplied observations",
        execution_token=EXECUTION_TOKEN,
        profile=MODEL_EVAL_PROFILE,
    )

    assert len(calls) == 2
    for call in calls:
        assert set(call["config"]["mcp_servers"]) == {"rca-progress"}


def test_runner_isolates_working_and_codex_home_directories(monkeypatch):
    calls = _capture_processes(monkeypatch)
    parent_home = os.environ.get("HOME")

    CodexSubprocessRunner().run("first", execution_token=EXECUTION_TOKEN)
    CodexSubprocessRunner().run("second", execution_token=EXECUTION_TOKEN)

    assert len(calls) == 4
    first, second = calls[0], calls[2]
    assert first["cwd"] != second["cwd"]
    assert first["env"]["CODEX_HOME"] != second["env"]["CODEX_HOME"]
    assert first["env"]["HOME"] == first["env"]["CODEX_HOME"]
    assert os.environ.get("HOME") == parent_home


def test_runner_keeps_local_aws_profile_files_reachable(monkeypatch, tmp_path):
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()
    config = aws_dir / "config"
    credentials = aws_dir / "credentials"
    config.write_text("[default]\nregion=us-east-1\n")
    credentials.write_text("[default]\naws_access_key_id=test\naws_secret_access_key=test\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = _capture_processes(monkeypatch)

    CodexSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert calls[0]["env"]["AWS_CONFIG_FILE"] == str(config)
    assert calls[0]["env"]["AWS_SHARED_CREDENTIALS_FILE"] == str(credentials)


def test_runner_passes_claim_context_only_to_the_child(monkeypatch):
    calls = _capture_processes(monkeypatch)

    CodexSubprocessRunner().run(
        "investigate",
        execution_token=EXECUTION_TOKEN,
        rca_id="rca-1",
        claim_token="claim-1",
        attempt=3,
    )

    assert len(calls) == 2
    for call in calls:
        env = call["env"]
        assert env["RCA_EXECUTION_TOKEN"] == EXECUTION_TOKEN
        assert env["RCA_SESSION_ID"] == "rca-1"
        assert env["RCA_CLAIM_TOKEN"] == "claim-1"
        assert env["RCA_ATTEMPT"] == "3"


def test_runner_returns_actionable_error_when_cli_is_missing(monkeypatch):
    monkeypatch.setattr(
        codex_subprocess_runner.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    result = CodexSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert result.success is False
    assert "Codex CLI not found" in result.result


def test_runner_kills_process_after_timeout(monkeypatch):
    process_holder = {}

    def _popen(args, **kwargs):
        process = FakeProcess(args)

        def _timeout(input: str, timeout: int):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

        process.communicate = _timeout
        process_holder["process"] = process
        return process

    monkeypatch.setattr(codex_subprocess_runner.subprocess, "Popen", _popen)

    result = CodexSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert result.success is False
    assert "timed out" in result.result
    assert process_holder["process"].killed is True


def test_runner_preserves_nonzero_exit_diagnostics(monkeypatch):
    _capture_processes(monkeypatch, [{"stderr": "provider failed", "returncode": 2}])

    result = CodexSubprocessRunner().run("investigate", execution_token=EXECUTION_TOKEN)

    assert result.success is False
    assert "rc=2" in result.result
    assert result.raw_output == "provider failed"


def test_jsonl_fallback_returns_the_last_agent_message():
    stdout = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}',
        ]
    )

    assert _last_agent_message(stdout) == "final"
