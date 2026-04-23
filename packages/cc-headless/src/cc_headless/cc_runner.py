from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cc_headless.config import CC_MAX_TURNS, CC_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_MCP_CONFIG_PATH = str(Path(__file__).resolve().parents[3] / "mcp-config.json")


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

    logger.info("CC CLI args: %s", json.dumps(args[1:]))

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

    logger.info("CC CLI stderr: %s", (proc.stderr or "")[:3000])
    logger.info("CC CLI stdout length: %d", len(proc.stdout or ""))

    if proc.returncode != 0:
        logger.error("CC CLI error: %s", (proc.stderr or "")[:3000])
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
