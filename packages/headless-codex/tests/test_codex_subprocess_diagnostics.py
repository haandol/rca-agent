"""Real local subprocess tests; no Codex, model, MCP or AWS endpoint is contacted."""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from structlog.testing import capture_logs

from headless_codex.adapters.secondary.codex import codex_diagnostics, codex_subprocess_runner
from headless_codex.adapters.secondary.codex.codex_subprocess_runner import CodexSubprocessRunner

EVENT = {
    "type": "item.started",
    "auth": "CANARY_AUTH",
    "item": {
        "type": "mcp_tool_call",
        "server": "cloudwatch",
        "tool": "execute_log_insights_query",
        "status": "in_progress",
        "arguments": {"password": "CANARY_ARGUMENT"},
        "result": {"token": "CANARY_RESULT"},
        "text": "CANARY_PRIVATE_TEXT",
    },
}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, {}),
        ({"error": None}, {"error_present": False}),
        ({"error": {}}, {"error_present": True}),
        ({"error": {"message": "CANARY_ERROR"}}, {"error_present": True}),
        ({"error": "CANARY_ERROR"}, {"error_present": True}),
        ({"result": {"isError": True}}, {"result_is_error": True}),
        ({"result": {"isError": False}}, {"result_is_error": False}),
        ({"result": {"isError": "CANARY_NOT_BOOLEAN"}}, {}),
        ({"result": {"isError": 1}}, {}),
        ({"result": {"isError": None}}, {}),
        ({"result": None}, {}),
        ({"result": ["CANARY_NOT_OBJECT"]}, {}),
    ],
)
def test_tool_error_metadata_preserves_presence_and_boolean_types_without_payloads(fields, expected):
    event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "completed", **fields}}
    metadata = codex_diagnostics.event_metadata(json.dumps(event).encode())
    assert metadata == {
        "event_type": "item.completed",
        "item_type": "mcp_tool_call",
        "status": "completed",
        **expected,
    }
    assert "CANARY" not in json.dumps(metadata)


@pytest.fixture
def local_runner(monkeypatch, tmp_path):
    """Replace only the child program; use the real runner, files, observer and communicate."""
    monkeypatch.setenv("CODEX_RUNTIME_HOME_ROOT", str(tmp_path / "homes"))
    monkeypatch.setattr(codex_subprocess_runner, "artifact_dir_for_token", lambda _token: tmp_path / "artifacts")
    monkeypatch.setattr(codex_subprocess_runner, "CODEX_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(codex_subprocess_runner, "_CANCEL_CHECK_INTERVAL", 0.02)

    def run(code, *, prompt="input", cancel_checker=None):
        monkeypatch.setattr(
            codex_subprocess_runner,
            "codex_exec_args",
            lambda _workspace, last_message: [sys.executable, "-u", "-c", code, str(last_message)],
        )
        return CodexSubprocessRunner().run(
            prompt, execution_token="a" * 32, profile="analysis-rca", cancel_checker=cancel_checker
        )

    return run


@pytest.mark.parametrize("exit_code", [0, 2])
def test_completed_tool_error_is_visible_in_logs_and_failure_preview_without_changing_exit_verdict(
    local_runner, exit_code
):
    event = json.loads(json.dumps(EVENT))
    event["type"] = "item.completed"
    event["item"].update(
        status="completed",
        error={"message": "CANARY_TOOL_ERROR"},
        result={"isError": True, "content": [{"text": "CANARY_TOOL_RESULT"}]},
    )
    payload = json.dumps(event) + "\n"
    code = f"""
import pathlib, sys
sys.stdin.read()
sys.stdout.write({payload!r})
pathlib.Path(sys.argv[1]).write_text("done")
sys.exit({exit_code})
"""
    with capture_logs() as logs:
        result = local_runner(code)
    metadata = next(entry for entry in logs if entry["event"] == "codex_jsonl_event")
    assert metadata["status"] == "completed"
    assert metadata["error_present"] is True
    assert metadata["result_is_error"] is True
    assert "CANARY" not in json.dumps(logs)
    assert result.success is (exit_code == 0)
    if result.success:
        assert result.raw_output == payload
    else:
        preview = json.loads(json.loads(result.raw_output)["stdout"]["text"])
        assert preview["error_present"] is True
        assert preview["result_is_error"] is True
        assert "CANARY" not in result.raw_output


def test_metadata_arrives_before_child_exit_and_private_files_preserve_success_output(local_runner, tmp_path):
    release = tmp_path / "release"
    payload = '{"type":[]}\n{"type":"CANARY_UNKNOWN_EVENT"}\n' + json.dumps(EVENT, ensure_ascii=False) + "\n"
    code = f"""
import os, pathlib, stat, sys, time
assert stat.S_IMODE(os.fstat(1).st_mode) == 0o600
assert stat.S_IMODE(os.fstat(2).st_mode) == 0o600
assert sys.stdin.read() == "input"
sys.stdout.write({payload!r})
sys.stdout.flush()
sys.stderr.write('Authorization: Bearer CANARY_STDERR\\n')
sys.stderr.flush()
while not pathlib.Path({str(release)!r}).exists():
    time.sleep(0.01)
pathlib.Path(sys.argv[1]).write_text("done")
"""
    with capture_logs() as logs, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(local_runner, code)
        try:
            until = time.monotonic() + 3
            while not any(event["event"] == "codex_jsonl_event" for event in logs) and time.monotonic() < until:
                time.sleep(0.01)
            events = [event for event in logs if event["event"] == "codex_jsonl_event"]
            assert events, "metadata was buffered until process exit"
            assert not future.done()
            assert events[0] == {
                "event": "codex_jsonl_event",
                "log_level": "info",
                "profile": "analysis-rca",
                "event_type": "item.started",
                "item_type": "mcp_tool_call",
                "server": "cloudwatch",
                "tool": "execute_log_insights_query",
                "status": "in_progress",
            }
        finally:
            release.touch()
        result = future.result(timeout=4)
    assert result.success
    assert result.result == "done"
    assert result.raw_output == payload
    assert "CANARY" not in json.dumps(logs)
    assert not list((tmp_path / "homes").glob("codex-home-*"))


def test_both_streams_can_exceed_pipe_capacity_before_child_reads_large_stdin(local_runner):
    stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "측정" * 100_000}}) + "\n"
    code = """
import json, pathlib, sys
event = {"type": "item.completed", "item": {"type": "agent_message", "text": "측정" * 100000}}
sys.stdout.write(json.dumps(event) + "\\n")
sys.stdout.flush()
sys.stderr.write("e" * 300000 + "\\n")
sys.stderr.flush()
assert len(sys.stdin.read()) == 300000
pathlib.Path(sys.argv[1]).write_text("large output complete")
"""
    with capture_logs() as logs:
        result = local_runner(code, prompt="p" * 300_000)
    assert result.success
    assert result.raw_output == stdout
    assert result.result == "large output complete"
    assert any(event["event"] == "codex_jsonl_event_omitted" for event in logs)
    assert all("stdout" not in event and "stderr" not in event for event in logs)


def test_nonzero_exit_retains_both_streams_redacted_without_logging_them(local_runner):
    payload = json.dumps(EVENT) + "\n"
    code = f"""
import sys
sys.stdin.read()
sys.stdout.write({payload!r})
sys.stderr.write('provider failed: Authorization: Bearer CANARY_STDERR\\n')
sys.exit(2)
"""
    with capture_logs() as logs:
        result = local_runner(code)
    assert not result.success
    assert "rc=2" in result.result
    diagnostics = json.loads(result.raw_output)
    assert "item.started" in diagnostics["stdout"]["text"]
    assert "provider failed" in diagnostics["stderr"]["text"]
    assert "CANARY_PRIVATE_TEXT" not in result.raw_output
    assert "arguments" not in result.raw_output
    for secret in ("CANARY_AUTH", "CANARY_ARGUMENT", "CANARY_RESULT", "CANARY_STDERR"):
        assert secret not in result.raw_output
    assert "CANARY" not in json.dumps(logs)


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel_keep_partial_streams_without_waiting_for_pipe_eof(
    local_runner, monkeypatch, tmp_path, cancel
):
    marker = tmp_path / "emitted"
    monkeypatch.setattr(codex_subprocess_runner, "CODEX_TIMEOUT_SECONDS", 0.6)
    payload = json.dumps(EVENT) + "\n"
    code = f"""
import pathlib, sys, time
sys.stdin.read()
sys.stdout.write({payload!r})
sys.stdout.write('{{"password":"CANARY_PARTIAL')
sys.stdout.flush()
sys.stderr.write('provider waiting: token=CANARY_STDERR\\n')
sys.stderr.flush()
pathlib.Path({str(marker)!r}).touch()
time.sleep(10)
"""
    started = time.monotonic()
    with capture_logs() as logs:
        result = local_runner(code, cancel_checker=marker.exists if cancel else None)
    assert time.monotonic() - started < 3
    assert not result.success
    assert result.cancelled is cancel
    assert ("cancelled" if cancel else "timed out") in result.result
    diagnostics = json.loads(result.raw_output)
    assert "item.started" in diagnostics["stdout"]["text"]
    assert "provider waiting" in diagnostics["stderr"]["text"]
    assert diagnostics["stdout"]["truncated"]
    assert diagnostics["stdout"]["omitted_lines"] == 1
    assert "CANARY_PARTIAL" not in result.raw_output
    assert "CANARY_STDERR" not in result.raw_output
    assert "CANARY" not in json.dumps(logs)
    assert not list((tmp_path / "homes").glob("codex-home-*"))


def test_large_failure_previews_are_bounded_and_cannot_leak_clipped_secrets(local_runner):
    code = """
import json, sys
sys.stdin.read()
sys.stdout.write(json.dumps({"password": "CANARY" * 100000}) + "\\n")
sys.stdout.write(json.dumps({"type": "turn.failed", "error": {"password": "CANARY_TAIL"}}) + "\\n")
sys.stderr.write("f" * 100000 + "\\n")
sys.stderr.write("password=CANARY_STDERR\\n")
sys.exit(3)
"""
    with capture_logs() as logs:
        result = local_runner(code)
    diagnostics = json.loads(result.raw_output)
    assert not result.success
    assert "turn.failed" in diagnostics["stdout"]["text"]
    for stream in diagnostics.values():
        assert len(stream["text"]) <= codex_diagnostics.MAX_PREVIEW_CHARS
        assert stream["truncated"]
    assert "CANARY" not in result.raw_output
    assert "CANARY" not in json.dumps(logs)
