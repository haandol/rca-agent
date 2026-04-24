"""rca-progress MCP server — 산출물 저장."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("rca-progress")

_SESSION_ID_PATH = Path("/tmp/rca-session-id")


def _rca_id() -> str:
    try:
        return _SESSION_ID_PATH.read_text().strip()
    except FileNotFoundError:
        return ""


@mcp.tool()
def save_artifact(filename: str, content: str) -> str:
    """분석 산출물을 /tmp/rca-{RCA_ID}/ 아래에 저장한다.

    Args:
        filename: 파일명. JSON 산출물은 .json 확장자, 보고서는 report.md.
                  예: scoping.json, hypotheses.json, validation-1.json, report.md
        content: 파일 내용 (JSON 문자열 또는 마크다운).
    """
    rca_id = _rca_id()
    base = f"/tmp/rca-{rca_id}" if rca_id else "/tmp/rca-unknown"
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, filename)
    with open(path, "w") as f:
        f.write(content)
    return json.dumps({"ok": True, "path": path})
