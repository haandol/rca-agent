from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

from cc_headless.config import CC_MAX_TURNS, CC_TIMEOUT_SECONDS

logger = structlog.get_logger()


def _find_file(name: str) -> str:
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        candidate = parent / name
        if candidate.exists():
            return str(candidate)
    return f"/app/{name}"


_MCP_CONFIG_PATH = _find_file("mcp-config.json")


@dataclass
class CcResult:
    success: bool
    result: str
    raw_output: str


def run_claude(prompt: str, *, mcp_config: str | None = None) -> CcResult:
    args = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--max-turns",
        str(CC_MAX_TURNS),
        "--mcp-config",
        mcp_config or _MCP_CONFIG_PATH,
    ]

    env = {**os.environ, "HOME": "/tmp"}

    logger.info("cc_cli_started", max_turns=CC_MAX_TURNS, mcp_config=mcp_config or _MCP_CONFIG_PATH)

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=CC_TIMEOUT_SECONDS,
            cwd="/app",
            env=env,
        )
    except subprocess.TimeoutExpired:
        return CcResult(success=False, result=f"Claude Code timed out after {CC_TIMEOUT_SECONDS}s", raw_output="")
    except FileNotFoundError:
        return CcResult(
            success=False,
            result="Claude Code CLI not found. Ensure @anthropic-ai/claude-code is installed globally.",
            raw_output="",
        )

    stdout_len = len(proc.stdout or "")
    stderr_len = len(proc.stderr or "")
    logger.info("cc_cli_finished", rc=proc.returncode, stdout_bytes=stdout_len, stderr_bytes=stderr_len)
    if proc.stderr:
        logger.info("cc_cli_stderr", stderr=proc.stderr[:5000])

    if proc.returncode != 0:
        logger.error("cc_cli_failed", rc=proc.returncode, stdout=(proc.stdout or "")[:5000])
        return CcResult(
            success=False,
            result=f"Claude Code process error (rc={proc.returncode})",
            raw_output=proc.stdout or proc.stderr or "",
        )

    stdout = proc.stdout or ""
    try:
        parsed = json.loads(stdout)
        result = parsed.get("result") or parsed.get("data", {}).get("result") or stdout
        if not isinstance(result, str):
            result = json.dumps(result)
        return CcResult(success=True, result=result, raw_output=stdout)
    except (json.JSONDecodeError, AttributeError):
        return CcResult(success=True, result=stdout.strip(), raw_output=stdout)
