"""rca-progress MCP server — 산출물 저장."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from fastmcp import FastMCP

from cc_headless.services.execution_context import RUN_TOKEN_ENV, artifact_dir_for_token

mcp = FastMCP("rca-progress")

_CANONICAL_ARTIFACTS = {
    "scoping.json",
    "hypotheses.json",
    "playbook.json",
    "report.md",
}
_VALIDATION_ARTIFACT = re.compile(r"validation-[1-9][0-9]*\.json")


def _artifact_dir() -> Path | None:
    token = os.environ.get(RUN_TOKEN_ENV, "")
    try:
        artifact_dir = artifact_dir_for_token(token)
    except ValueError:
        return None
    return artifact_dir if artifact_dir.is_dir() and not artifact_dir.is_symlink() else None


def _is_allowed_filename(filename: str) -> bool:
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        return False
    return filename in _CANONICAL_ARTIFACTS or _VALIDATION_ARTIFACT.fullmatch(filename) is not None


@mcp.tool()
def save_artifact(filename: str, content: str) -> str:
    """분석 산출물을 현재 RCA 실행의 격리된 디렉터리에 저장한다.

    Args:
        filename: 파일명. JSON 산출물은 .json 확장자, 보고서는 report.md.
                  예: scoping.json, hypotheses.json, validation-1.json, report.md
        content: 파일 내용 (JSON 문자열 또는 마크다운).
    """
    if not _is_allowed_filename(filename):
        return json.dumps({"ok": False, "error": f"unsupported artifact filename: {filename}"})

    base = _artifact_dir()
    if base is None:
        return json.dumps({"ok": False, "error": "missing or invalid RCA execution context"})

    path = base / filename
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=base,
            prefix=f".{filename}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return json.dumps({"ok": True, "path": str(path)})
