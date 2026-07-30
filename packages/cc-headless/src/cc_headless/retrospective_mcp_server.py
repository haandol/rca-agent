"""플레이북 회고 MCP server.

회고는 실행 증거로 절차를 교정한다. 이 서버는 갱신안을 받아 격리 디렉터리에 저장할
뿐이고, **병합은 서버가 수행한다** — 삭제 금지를 프롬프트 지시에 맡기면 모델이 필드를
누락할 때 축적이 조용히 사라진다.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from fastmcp import FastMCP

from cc_headless.services.execution_workspace import (
    EXECUTION_TOKEN_ENV,
    retrospective_path_for_token,
)

mcp = FastMCP("playbook-retrospective")

_EXECUTION_STEP_FIELDS = ("step_id", "intent", "action", "success_criteria")


def _target_path() -> Path | None:
    token = os.environ.get(EXECUTION_TOKEN_ENV, "")
    try:
        path = retrospective_path_for_token(token)
    except ValueError:
        return None
    return path if path.parent.is_dir() and not path.parent.is_symlink() else None


def _write_atomic(path: Path, content: str) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
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


@mcp.tool()
def save_playbook_update(update_json: str, rationale: str) -> str:
    """실행 증거에서 도출한 플레이북 갱신안을 저장한다.

    바꿀 필드와 절차만 담는다. 담지 않은 것은 기존 값이 유지되므로 전체를 다시 쓰지
    않아도 되며, 반대로 어떤 필드를 비워도 그 필드가 지워지지는 않는다.

    Args:
        update_json: 갱신안 JSON 객체. `execution_steps` 에는 교정할 절차만 담고
                     `step_id` 는 기존 식별자를 그대로 쓴다. 새 절차를 추가할 때만
                     새 `step_id` 를 만든다.
        rationale: 각 변경이 실행 증거의 무엇에서 도출되었는지. 일시적 오류는 절차
                   결함이 아니므로 교정 근거가 될 수 없다.
    """
    if not isinstance(rationale, str) or not rationale.strip():
        return json.dumps({"ok": False, "error": "rationale is required"}, ensure_ascii=False)

    try:
        update = json.loads(update_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return json.dumps({"ok": False, "error": f"update_json is not valid JSON: {exc}"}, ensure_ascii=False)
    if not isinstance(update, dict):
        return json.dumps({"ok": False, "error": "update_json must be a JSON object"}, ensure_ascii=False)

    steps = update.get("execution_steps")
    if steps is not None:
        if not isinstance(steps, list):
            return json.dumps({"ok": False, "error": "execution_steps must be a list"}, ensure_ascii=False)
        for step in steps:
            if not isinstance(step, dict) or not str(step.get("step_id") or "").strip():
                return json.dumps(
                    {
                        "ok": False,
                        "error": "every execution step needs a step_id naming the step it corrects",
                    },
                    ensure_ascii=False,
                )
            unknown = set(step) - set(_EXECUTION_STEP_FIELDS)
            if unknown:
                return json.dumps(
                    {"ok": False, "error": f"unsupported execution step fields: {', '.join(sorted(unknown))}"},
                    ensure_ascii=False,
                )

    path = _target_path()
    if path is None:
        return json.dumps({"ok": False, "error": "missing execution context"}, ensure_ascii=False)

    _write_atomic(
        path,
        json.dumps({"update": update, "rationale": rationale}, ensure_ascii=False),
    )
    return json.dumps({"ok": True}, ensure_ascii=False)
