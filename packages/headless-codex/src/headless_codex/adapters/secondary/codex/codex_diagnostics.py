"""Private stream previews and content-free observation of Codex JSONL output."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from threading import Event
from typing import BinaryIO

from headless_codex.services.execution_evidence import redact

MAX_EVENT_BYTES = 64 * 1024
MAX_PREVIEW_BYTES = 128 * 1024
MAX_PREVIEW_CHARS = 20_000
_OBSERVE_INTERVAL = 0.2
_EVENT_TYPES = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)
_ITEM_TYPES = frozenset(
    {
        "agent_message",
        "reasoning",
        "command_execution",
        "mcp_tool_call",
        "collab_tool_call",
        "file_change",
        "web_search",
        "todo_list",
    }
)
_STATUSES = frozenset({"in_progress", "completed", "failed", "cancelled"})
_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.:-]{0,127}")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"""(?ix)(?<![\w.-])[\w.-]*(?:secret|password|passwd|token|credential|api[-_.]?key|access[-_.]?key|private[-_.]?key|auth)"""
    r"""[\w.-]*["']?\s*[=:]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]"']+)"""
)


def event_metadata(line: bytes) -> dict | None:
    """Allow kinds, names, status and error booleans; never copy arbitrary payload fields."""
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str) or event["type"] not in _EVENT_TYPES:
        return None
    metadata = {"event_type": event["type"]}
    item = event.get("item")
    if not isinstance(item, dict):
        return metadata
    if isinstance(item.get("type"), str) and item["type"] in _ITEM_TYPES:
        metadata["item_type"] = item["type"]
    if isinstance(item.get("status"), str) and item["status"] in _STATUSES:
        metadata["status"] = item["status"]
    if item.get("type") == "mcp_tool_call":
        if "error" in item:
            metadata["error_present"] = item["error"] is not None
        result = item.get("result")
        if isinstance(result, dict) and isinstance(result.get("isError"), bool):
            metadata["result_is_error"] = result["isError"]
        for name in ("server", "tool"):
            value = item.get(name)
            if isinstance(value, str) and _NAME.fullmatch(value) and redact(value) == value:
                metadata[name] = value
    return metadata


def observe_jsonl(path: Path, done: Event, log) -> None:
    """Tail a separately opened file; never touch the child's descriptor offset or its pipes."""
    pending = b""
    dropping = False
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(MAX_EVENT_BYTES)
                if not chunk:
                    if done.is_set():
                        if pending and not dropping and (metadata := event_metadata(pending)):
                            log.info("codex_jsonl_event", **metadata)
                        return
                    done.wait(_OBSERVE_INTERVAL)
                    continue
                pieces = chunk.split(b"\n")
                for index, piece in enumerate(pieces):
                    complete = index < len(pieces) - 1
                    if not dropping:
                        pending += piece
                        if len(pending) > MAX_EVENT_BYTES:
                            pending = b""
                            dropping = True
                            log.info("codex_jsonl_event_omitted", reason="line_too_large")
                    if complete:
                        if not dropping and pending and (metadata := event_metadata(pending)):
                            log.info("codex_jsonl_event", **metadata)
                        pending = b""
                        dropping = False
    except Exception as exc:
        # Diagnostics cannot fail the analysis or expose an exception's embedded output.
        log.warning("codex_jsonl_observer_failed", error_type=type(exc).__name__)


def _preview(stream: BinaryIO, *, interrupted: bool, metadata_only: bool = False) -> dict:
    """Redact complete lines from a bounded tail before imposing the retained character cap.

    Drop clipped boundary lines and malformed JSON instead of leaking a partly printed credential.
    This is a diagnostic preview, never a complete JSONL stream or artifact.
    """
    stream.seek(0, 2)
    total = stream.tell()
    offset = max(0, total - MAX_PREVIEW_BYTES)
    stream.seek(offset)
    tail = stream.read(MAX_PREVIEW_BYTES)
    omitted = 0
    if offset:
        _, separator, tail = tail.partition(b"\n")
        if not separator:
            tail = b""
        omitted += 1
    lines = tail.decode("utf-8", errors="replace").splitlines(keepends=True)
    if interrupted and lines and not lines[-1].endswith(("\n", "\r")):
        lines.pop()
        omitted += 1
    safe_lines = []
    for line in lines:
        if metadata_only:
            metadata = event_metadata(line.encode("utf-8"))
            if metadata is None:
                omitted += 1
            else:
                safe_lines.append(json.dumps(metadata, ensure_ascii=False) + "\n")
            continue
        if line.lstrip().startswith(("{", "[")):
            try:
                json.loads(line)
            except (ValueError, RecursionError):
                omitted += 1
                continue
        try:
            # A non-secret prefix such as "waiting: token=..." can consume the next
            # assignment in the shared redactor. Inspect credential assignments separately.
            safe_lines.append(_CREDENTIAL_ASSIGNMENT.sub(lambda match: redact(match[0]), redact(line)))
        except (ValueError, RecursionError):
            omitted += 1
    safe = "".join(safe_lines)
    return {
        "text": safe[-MAX_PREVIEW_CHARS:],
        "total_bytes": total,
        "retained_chars": min(len(safe), MAX_PREVIEW_CHARS),
        "omitted_lines": omitted,
        "truncated": bool(offset or omitted or len(safe) > MAX_PREVIEW_CHARS),
    }


def failure_diagnostics(stdout: Path, stderr: Path, *, interrupted: bool) -> str:
    """Return bounded redacted previews of both private streams without logging their contents."""
    with stdout.open("rb") as out, stderr.open("rb") as err:
        return json.dumps(
            {
                # The pipeline may log raw_output on failure. Never return model payloads there.
                "stdout": _preview(out, interrupted=interrupted, metadata_only=True),
                "stderr": _preview(err, interrupted=interrupted),
            },
            ensure_ascii=False,
        )


def failed_role_diagnostics(rca_output: str, report_output: str = "") -> str:
    """A failed second role must not return an unlimited, unredacted successful first-role trace."""
    return json.dumps(
        {
            "rca": _preview(io.BytesIO(rca_output.encode("utf-8")), interrupted=False, metadata_only=True),
            # The second role already returned our bounded, redacted failure envelope.
            "report": json.loads(report_output) if report_output else {},
        },
        ensure_ascii=False,
    )
