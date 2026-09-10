import json
import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from headless_codex.adapters.secondary.codex import codex_subprocess_runner
from headless_codex.adapters.secondary.codex.codex_harness import MODEL_EVAL_PROFILE
from headless_codex.adapters.secondary.codex.codex_subprocess_runner import (
    CodexSubprocessRunner,
    _last_agent_message,
)

EXECUTION_TOKEN = "a" * 32


@pytest.fixture(autouse=True)
def isolated_codex_runtime_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_RUNTIME_HOME_ROOT", str(tmp_path / "codex-runs"))


@pytest.fixture(autouse=True)
def verified_analysis_for_fake_process(monkeypatch, analysis_artifacts, save_validation, judgment):
    """Give fake successful RCA processes real terminal artifacts for the report handoff."""
    save_validation(1, confirmed=[judgment("root", 0.95, reasoning="[owned-blocker] measured lock")])
    monkeypatch.setattr(codex_subprocess_runner, "artifact_dir_for_token", lambda _token: analysis_artifacts)


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
        process = FakeProcess(args, **(queued.pop(0) if queued else {}))
        call["process"] = process
        return process

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
    assert provider["auth"]["args"] == ["-m", "headless_codex.bedrock_token"]


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
    assert context_vars.issubset(rca["rca-progress"]["env_vars"])
    assert context_vars.issubset(report["rca-progress"]["env_vars"])
    assert "DYNAMODB_TABLE_NAME" in rca["rca-progress"]["env_vars"]
    assert "DYNAMODB_TABLE_NAME" in report["rca-progress"]["env_vars"]
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


@pytest.mark.parametrize("profile", ["analysis", "model-eval"])
@pytest.mark.parametrize("rca_seconds,expected_processes", [(70, 2), (100, 1), (101, 1)])
def test_roles_share_one_monotonic_deadline(monkeypatch, profile, rca_seconds, expected_processes):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(codex_subprocess_runner, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(codex_subprocess_runner, "CODEX_TIMEOUT_SECONDS", 100)
    original_communicate = FakeProcess.communicate
    durations = iter([rca_seconds, 1])

    def communicate(self, input, timeout):
        output = original_communicate(self, input, timeout)
        clock.now += next(durations)
        return output

    monkeypatch.setattr(FakeProcess, "communicate", communicate)
    calls = _capture_processes(monkeypatch, [{"result": "RCA evidence", "stdout": "rca trace"}])
    result = CodexSubprocessRunner().run(
        "rca-only input",
        report_prompt="report-only input",
        execution_token=EXECUTION_TOKEN,
        profile=profile,
    )

    assert len(calls) == expected_processes
    assert calls[0]["process"].timeout == 100
    if expected_processes == 2:
        assert calls[1]["process"].timeout == 30
        assert "report-only input" in calls[1]["process"].input
        assert "rca-only input" not in calls[1]["process"].input
        assert "RCA evidence" in calls[1]["process"].input
        assert result.success
    else:
        assert not result.success
        assert "timed out" in result.result
        assert "rca trace" in result.raw_output


@pytest.mark.parametrize("expire_during_setup", [False, True])
def test_shared_budget_includes_workspace_and_process_startup(monkeypatch, expire_during_setup):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(codex_subprocess_runner, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(codex_subprocess_runner, "CODEX_TIMEOUT_SECONDS", 100)
    prepare = codex_subprocess_runner.prepare_workspace

    def slow_setup(workspace, profile):
        prepare(workspace, profile)
        clock.now += 50 if expire_during_setup else 10

    monkeypatch.setattr(codex_subprocess_runner, "prepare_workspace", slow_setup)
    calls = _capture_processes(monkeypatch)
    popen = codex_subprocess_runner.subprocess.Popen

    def slow_start(*args, **kwargs):
        process = popen(*args, **kwargs)
        clock.now += 5
        return process

    monkeypatch.setattr(codex_subprocess_runner.subprocess, "Popen", slow_start)
    result = CodexSubprocessRunner().run("input", execution_token=EXECUTION_TOKEN)

    if expire_during_setup:
        assert not result.success
        assert len(calls) == 1
        assert calls[0]["process"].timeout == 45
    else:
        assert result.success
        assert [call["process"].timeout for call in calls] == [85, 70]


def test_report_is_killed_at_remaining_deadline(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(codex_subprocess_runner, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(codex_subprocess_runner, "CODEX_TIMEOUT_SECONDS", 100)
    communicate = FakeProcess.communicate

    def consume_budget(self, input, timeout):
        if clock.now == 0:
            clock.now = 99
            return communicate(self, input, timeout)
        assert timeout == 1
        raise subprocess.TimeoutExpired("codex", timeout)

    monkeypatch.setattr(FakeProcess, "communicate", consume_budget)
    calls = _capture_processes(monkeypatch, [{"stdout": "RCA diagnostics"}])
    result = CodexSubprocessRunner().run("input", execution_token=EXECUTION_TOKEN)

    assert len(calls) == 2
    assert calls[1]["process"].killed
    assert not result.success
    assert "timed out" in result.result
    assert "RCA diagnostics" in result.raw_output


@pytest.mark.parametrize("profile", ["analysis-rca", "analysis-report", "model-eval-rca", "model-eval-report"])
def test_direct_specialist_profiles_keep_their_full_process_timeout(monkeypatch, profile):
    def unexpected_clock():
        raise AssertionError("a direct single-process call does not use the shared deadline")

    monkeypatch.setattr(codex_subprocess_runner, "time", SimpleNamespace(monotonic=unexpected_clock))
    calls = _capture_processes(monkeypatch)
    result = CodexSubprocessRunner().run("input", execution_token=EXECUTION_TOKEN, profile=profile)

    assert result.success
    assert len(calls) == 1
    assert calls[0]["process"].timeout == codex_subprocess_runner.CODEX_TIMEOUT_SECONDS


def test_cancelled_rca_does_not_start_report(monkeypatch):
    calls = _capture_processes(monkeypatch, [{"returncode": -15}])
    result = CodexSubprocessRunner().run("input", execution_token=EXECUTION_TOKEN)

    assert result.cancelled
    assert not result.success
    assert len(calls) == 1


def test_report_receives_server_state_even_if_model_summary_conflicts(monkeypatch):
    """The handoff includes the actual selected root and CLOSED statuses with explicit authority."""
    calls = _capture_processes(monkeypatch, [{"result": "all alternatives rejected; root is high-cpu"}])
    result = CodexSubprocessRunner().run("input", execution_token=EXECUTION_TOKEN)
    assert result.success
    report_input = calls[1]["process"].input
    state = json.JSONDecoder().raw_decode(report_input.split("[서버 검증 유효 상태 — 판정의 권위]\n")[1])[0]
    assert state["selected_hypothesis_id"] == "root"
    assert state["hypotheses"][0]["fault_type"] == "unsupported"
    assert state["hypotheses"][0]["reasoning"] == "[owned-blocker] measured lock"
    assert [h["status"] for h in state["hypotheses"]] == ["confirmed", "closed", "closed"]
    assert "위 요약이 충돌하면 이 상태가 우선" in report_input


@pytest.mark.parametrize("corruption", ["missing", "decision"])
def test_unverifiable_analysis_stops_before_report(monkeypatch, analysis_artifacts, corruption):
    """A successful CLI message cannot substitute for missing or tampered server state."""
    artifact = analysis_artifacts / "validation-1.json"
    if corruption == "missing":
        artifact.unlink()
    else:
        value = json.loads(artifact.read_text())
        value["server_decision"]["selected_hypothesis_id"] = "third"
        artifact.write_text(json.dumps(value))
    calls = _capture_processes(monkeypatch, [{"stdout": "raw RCA trace"}])
    result = CodexSubprocessRunner().run("input", execution_token=EXECUTION_TOKEN)
    assert not result.success
    assert len(calls) == 1
    assert "effective state could not be verified" in result.result
    assert result.raw_output == "raw RCA trace"
