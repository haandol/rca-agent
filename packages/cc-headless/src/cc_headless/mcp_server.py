"""격리 산출물 저장 MCP server.

이 서버는 분석 실행에만 제공되며 쓰기 도구를 노출하지 않는다. 복구는 사용자 승인
뒤 별도 실행 에이전트가 수행한다.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from fastmcp import FastMCP

from cc_headless.services.artifact_validation import (
    ArtifactValidationError,
    validate_artifact_shape,
)
from cc_headless.services.execution_context import (
    RUN_TOKEN_ENV,
    artifact_dir_for_token,
)

mcp = FastMCP("rca-progress")

_VALIDATION_ARTIFACT = re.compile(r"validation-[1-9][0-9]*\.json")

# 산출물은 역할별 도구로 갈라 저장한다. 한 도구가 모든 파일명을 받으면 어느 역할이
# 무엇을 썼는지 서버가 알 수 없고, 분리는 프롬프트 지시로만 남는다. 도구를 나누면
# 에이전트에 부여된 도구 목록이 그대로 경계가 되어, 분석 역할이 리포트를 쓰는 경로가
# 도구 부재로 막힌다.
_ANALYSIS_ARTIFACTS = {
    "scoping.json",
    "hypotheses.json",
}
_REPORT_ARTIFACTS = {
    "playbook.json",
    "report.md",
}


def _artifact_dir() -> Path | None:
    token = os.environ.get(RUN_TOKEN_ENV, "")
    try:
        artifact_dir = artifact_dir_for_token(token)
    except ValueError:
        return None
    return artifact_dir if artifact_dir.is_dir() and not artifact_dir.is_symlink() else None


def _is_allowed_filename(filename: str, allowed: set[str], *, allow_validation: bool) -> bool:
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        return False
    if filename in allowed:
        return True
    return allow_validation and _VALIDATION_ARTIFACT.fullmatch(filename) is not None


def _write_artifact(base: Path, filename: str, content: str) -> Path:
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
    return path


def _save(filename: str, content: str, allowed: set[str], *, allow_validation: bool) -> str:
    if not _is_allowed_filename(filename, allowed, allow_validation=allow_validation):
        return json.dumps(
            {
                "ok": False,
                "error": (
                    f"unsupported artifact filename for this role: {filename}. "
                    "이 역할이 저장할 산출물이 아니다 — 담당 역할이 저장해야 한다."
                ),
            },
            ensure_ascii=False,
        )

    base = _artifact_dir()
    if base is None:
        return json.dumps({"ok": False, "error": "missing or invalid RCA execution context"})

    # Reject a malformed artifact now rather than at the completion gate, where
    # the run has already ended and the agent can no longer correct it.
    try:
        validate_artifact_shape(filename, content)
    except ArtifactValidationError as exc:
        return json.dumps(
            {"ok": False, "error": f"artifact rejected: {exc}. Fix the content and save again."},
            ensure_ascii=False,
        )

    path = _write_artifact(base, filename, content)
    return json.dumps({"ok": True, "path": str(path)})


@mcp.tool()
def save_analysis_artifact(filename: str, content: str) -> str:
    """RCA 분석 산출물을 현재 실행의 격리된 디렉터리에 저장한다.

    이 도구는 분석 역할의 산출물만 받는다. 리포트와 플레이북은 보고 역할이 자신의
    도구로 저장한다.

    Args:
        filename: scoping.json, hypotheses.json, validation-{N}.json 중 하나.
        content: 파일 내용 (JSON 문자열).
    """
    return _save(filename, content, _ANALYSIS_ARTIFACTS, allow_validation=True)


@mcp.tool()
def save_report_artifact(filename: str, content: str) -> str:
    """리포트 산출물을 현재 실행의 격리된 디렉터리에 저장한다.

    이 도구는 보고 역할의 산출물만 받는다. 분석 산출물은 분석 역할이 자신의 도구로
    저장한다.

    Args:
        filename: report.md 또는 playbook.json.
        content: 파일 내용 (마크다운 또는 JSON 문자열).
    """
    return _save(filename, content, _REPORT_ARTIFACTS, allow_validation=False)
