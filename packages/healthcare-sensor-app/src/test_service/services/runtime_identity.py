"""Read only task identity from the trusted local ECS v4 endpoint at startup."""

import asyncio
import json
import os
import re

METADATA_TIMEOUT_SECONDS = 2.0
MAX_METADATA_BYTES = 256 * 1024
_METADATA_BASE = re.compile(r"http://169\.254\.170\.2/v4/[A-Za-z0-9-]{1,128}/?")
_ARN_PREFIX = r"arn:aws(?:-[a-z0-9-]+)?:ecs:[a-z0-9-]+:[0-9]{12}:"
_FIELD_PATTERNS = {
    "TaskARN": rf"{_ARN_PREFIX}task/(?:[A-Za-z0-9_-]+/)?[a-f0-9]{{32}}",
    "Cluster": rf"(?:[A-Za-z0-9_-]{{1,255}}|{_ARN_PREFIX}cluster/[A-Za-z0-9_-]{{1,255}})",
    "Family": r"[A-Za-z0-9_-]{1,255}",
    "Revision": r"[0-9]{1,20}",
}


async def _read_task_metadata(base: str) -> dict:
    """Use bounded HTTP/1.0 without DNS, proxies, redirects, tags or credentials.

    The caller enforces the total deadline, including slow response bodies.
    Unsupported framing fails closed instead of following another endpoint.
    """
    path = base.removeprefix("http://169.254.170.2").rstrip("/") + "/task"
    reader, writer = await asyncio.open_connection("169.254.170.2", 80, limit=8192)
    try:
        writer.write(f"GET {path} HTTP/1.0\r\nHost: 169.254.170.2\r\nConnection: close\r\n\r\n".encode("ascii"))
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        lines = header.split(b"\r\n")
        status = lines[0].split()
        if len(header) > 8192 or len(status) < 2 or status[0] not in (b"HTTP/1.0", b"HTTP/1.1") or status[1] != b"200":
            raise ValueError("Invalid metadata response")
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            if name.lower() == b"transfer-encoding" or (
                name.lower() == b"content-encoding" and value.strip().lower() != b"identity"
            ):
                raise ValueError("Unsupported metadata encoding")
        body = bytearray()
        while chunk := await reader.read(min(65536, MAX_METADATA_BYTES + 1 - len(body))):
            body.extend(chunk)
            if len(body) > MAX_METADATA_BYTES:
                raise ValueError("Metadata response too large")
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError("Invalid metadata object")
        return document
    finally:
        # Closing does not wait for the remote endpoint or leave a worker thread.
        writer.close()


async def runtime_identity() -> dict:
    """Return allowlisted observed fields or safe absence; never synthesize identity."""
    record = {"event": "ecs_runtime_identity", "status": "unavailable"}
    base = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not base:
        return {**record, "reason": "not_ecs"}
    if not _METADATA_BASE.fullmatch(base):
        return {**record, "reason": "invalid_endpoint", "error_type": "ValueError"}
    try:
        async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
            document = await _read_task_metadata(base)
        identity = {
            name: value
            for name, pattern in _FIELD_PATTERNS.items()
            if isinstance(value := document.get(name), str) and re.fullmatch(pattern, value)
        }
        missing = [name for name in _FIELD_PATTERNS if name not in identity]
        if missing:
            return {
                **record,
                **identity,
                "status": "partial" if identity else "unavailable",
                "reason": "incomplete_metadata",
                "missing_fields": missing,
            }
        return {**record, **identity, "status": "available"}
    except Exception as exc:
        return {**record, "reason": "metadata_unavailable", "error_type": type(exc).__name__}
