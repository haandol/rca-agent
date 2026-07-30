from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import structlog

from cc_headless.config.settings import CC_TIMEOUT_SECONDS
from cc_headless.ports.dto.models import CcResult
from cc_headless.ports.interfaces.cc_runner import CcRunnerPort
from cc_headless.services.execution_context import (
    ATTEMPT_ENV,
    CLAIM_TOKEN_ENV,
    RCA_ID_ENV,
    RUN_TOKEN_ENV,
    artifact_dir_for_token,
)

logger = structlog.get_logger()

_CANCEL_CHECK_INTERVAL = 15


def _find_file(name: str) -> str:
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        candidate = parent / name
        if candidate.exists():
            return str(candidate)
    return f"/app/{name}"


_MCP_CONFIG_PATH = _find_file("mcp-config.json")
_WORKSPACE_SOURCE = Path(_MCP_CONFIG_PATH).parent
_PACKAGE_ROOT_PLACEHOLDER = "{{PACKAGE_ROOT}}"
_ROOT_AGENT = "orchestrator"
_BUILTIN_TOOLS = ("Agent", "Skill")
# The analysis run is read-only: no tool here changes a service.
_ALLOWED_TOOLS = (
    *_BUILTIN_TOOLS,
    "mcp__aws-knowledge__*",
    "mcp__cloudwatch__*",
    "mcp__cloudtrail__*",
    "mcp__github__*",
    "mcp__rca-progress__save_artifact",
)


def _render_mcp_config(source: str, dest_dir: Path) -> str:
    """Resolve packaged MCP server references against the installed package root.

    The committed config refers to the packaged MCP server through a placeholder so
    the same harness starts from a local checkout and from the container image.
    """
    text = Path(source).read_text()
    if _PACKAGE_ROOT_PLACEHOLDER not in text:
        return source
    dest = dest_dir / "mcp-config.json"
    dest.write_text(text.replace(_PACKAGE_ROOT_PLACEHOLDER, str(_WORKSPACE_SOURCE)))
    return str(dest)


def _prepare_workspace(path: Path) -> None:
    claude_md = _WORKSPACE_SOURCE / "CLAUDE.md"
    skills = _WORKSPACE_SOURCE / ".claude"
    if claude_md.is_file():
        shutil.copy2(claude_md, path / "CLAUDE.md")
    if skills.is_dir():
        shutil.copytree(skills, path / ".claude")


def _watch_cancel(
    proc: subprocess.Popen,
    stop_event: Event,
    cancel_checker: Callable[[], bool],
) -> None:
    while not stop_event.wait(_CANCEL_CHECK_INTERVAL):
        if cancel_checker():
            logger.info("cancel_detected_killing_cc_process")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return


class CcSubprocessRunner(CcRunnerPort):
    def run(
        self,
        prompt: str,
        *,
        execution_token: str,
        mcp_config: str | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        rca_id: str | None = None,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> CcResult:
        artifact_dir_for_token(execution_token)

        with (
            TemporaryDirectory(prefix="cc-workspace-") as workspace,
            TemporaryDirectory(prefix="cc-home-") as home,
        ):
            workspace_path = Path(workspace)
            _prepare_workspace(workspace_path)
            resolved_mcp_config = _render_mcp_config(mcp_config or _MCP_CONFIG_PATH, Path(home))
            args = [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--dangerously-skip-permissions",
                "--mcp-config",
                resolved_mcp_config,
                "--strict-mcp-config",
                "--no-session-persistence",
                "--agent",
                _ROOT_AGENT,
                "--tools",
                ",".join(_BUILTIN_TOOLS),
                "--allowedTools",
                ",".join(_ALLOWED_TOOLS),
            ]

            logger.info("cc_cli_started", mcp_config=resolved_mcp_config)

            env = {
                **os.environ,
                "HOME": home,
                "CLAUDE_CONFIG_DIR": str(Path(home) / ".claude"),
                RUN_TOKEN_ENV: execution_token,
            }
            if rca_id:
                env[RCA_ID_ENV] = rca_id
            if claim_token:
                env[CLAIM_TOKEN_ENV] = claim_token
            if attempt is not None:
                env[ATTEMPT_ENV] = str(attempt)

            try:
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=workspace,
                    env=env,
                )
            except FileNotFoundError:
                return CcResult(
                    success=False,
                    result="Claude Code CLI not found. Ensure @anthropic-ai/claude-code is installed globally.",
                    raw_output="",
                )

            stop_event = Event()
            if cancel_checker:
                Thread(target=_watch_cancel, args=(proc, stop_event, cancel_checker), daemon=True).start()

            try:
                stdout, stderr = proc.communicate(timeout=CC_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                stop_event.set()
                return CcResult(
                    success=False,
                    result=f"Claude Code timed out after {CC_TIMEOUT_SECONDS}s",
                    raw_output="",
                )
            finally:
                stop_event.set()

        stdout_len = len(stdout or "")
        stderr_len = len(stderr or "")
        logger.info("cc_cli_finished", rc=proc.returncode, stdout_bytes=stdout_len, stderr_bytes=stderr_len)
        if stderr:
            logger.info("cc_cli_stderr", stderr=stderr[:5000])

        if proc.returncode == -15:
            return CcResult(success=False, result="Process terminated (cancelled)", raw_output="", cancelled=True)

        if proc.returncode != 0:
            logger.error("cc_cli_failed", rc=proc.returncode, stdout=(stdout or "")[:5000])
            return CcResult(
                success=False,
                result=f"Claude Code process error (rc={proc.returncode})",
                raw_output=stdout or stderr or "",
            )

        stdout = stdout or ""
        try:
            parsed = json.loads(stdout)
            result = parsed.get("result") or parsed.get("data", {}).get("result") or stdout
            if not isinstance(result, str):
                result = json.dumps(result)
            return CcResult(success=True, result=result, raw_output=stdout)
        except (json.JSONDecodeError, AttributeError):
            return CcResult(success=True, result=stdout.strip(), raw_output=stdout)
