"""Token-local model inputs and immutable role results; published authority stays with the server."""

from __future__ import annotations

import json
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from headless_codex.services.execution_context import artifact_dir_for_token

CONTEXT_NAME = "analysis-parts-context.json"
CONTROL_NAME = "analysis-parts-control.json"
RESULT_FILES = {
    "recovery": "recovery-result.json",
    "code_proposal": "code-proposal.json",
    "operations": "operations-result.json",
}


def fixed_path(token: str, name: str) -> Path:
    """Resolve only server-selected filenames in this execution's existing private directory."""
    base = artifact_dir_for_token(token)
    path = base / name
    if not base.is_dir() or base.is_symlink() or path.is_symlink():
        raise ValueError("analysis part workspace is missing or unsafe")
    return path


def _metadata_json(value):
    """Represent DynamoDB numeric metadata as JSON numbers without altering signed S3 payload bytes."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite analysis metadata")
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"unsupported analysis metadata type: {type(value).__name__}")


def write_once(token: str, name: str, value: dict) -> None:
    """Publish complete JSON once; exact retries are harmless and changed results are rejected."""
    path = fixed_path(token, name)
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_metadata_json
    ).encode()
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("analysis part result is already immutable")
        return
    fd, temporary = tempfile.mkstemp(prefix=".analysis-part-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("concurrent analysis part result differs") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_object(token: str, name: str) -> dict:
    """Missing or malformed source is an error, never an empty successful role result."""
    value = json.loads(fixed_path(token, name).read_bytes())
    if not isinstance(value, dict):
        raise ValueError("analysis part artifact must be an object")
    return value


def activate_role(token: str, role: str, deadline: float) -> None:
    """Carry the remaining original budget to model tools, without starting a fresh stage clock."""
    from headless_codex.services.execution_workspace import write_observation_json

    remaining = max(0.0, deadline - time.monotonic())
    write_observation_json(
        fixed_path(token, CONTROL_NAME),
        {
            "role": role,
            "deadline_epoch": time.time() + remaining,
            "active": remaining > 0,
        },
    )


def require_role(token: str, role: str) -> dict:
    """Reject cross-role or expired local writes before the parent performs its publication fencing."""
    control = read_object(token, CONTROL_NAME)
    if (
        control.get("role") != role
        or control.get("active") is not True
        or time.time() >= control.get("deadline_epoch", 0)
    ):
        raise ValueError("analysis part role is inactive or expired")
    return read_object(token, CONTEXT_NAME)
