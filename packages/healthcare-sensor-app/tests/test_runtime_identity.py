"""Offline coverage of trusted metadata, privacy, deadlines and startup identity."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from test_service.services import runtime_identity as identity

BASE = "http://169.254.170.2/v4/6d7b8c9d-1234-5678-9876-0123456789ab"
TASK = "arn:aws:ecs:us-east-1:123456789012:task/Healthcare/0123456789abcdef0123456789abcdef"
FIELDS = {"TaskARN": TASK, "Cluster": "Healthcare", "Family": "Healthcare", "Revision": "17"}


@pytest.fixture(autouse=True)
def no_real_metadata(monkeypatch):
    """Keep every test offline even if the host happens to run inside ECS."""
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    connect = AsyncMock(side_effect=AssertionError("Unexpected network access"))
    monkeypatch.setattr(identity.asyncio, "open_connection", connect)
    return connect


def metadata_response(monkeypatch, payload, *, header=b"HTTP/1.0 200 OK\r\n\r\n"):
    """Serve an in-memory HTTP response without credentials, DNS or sockets."""
    reader = asyncio.StreamReader()
    reader.feed_data(header + (json.dumps(payload).encode() if not isinstance(payload, bytes) else payload))
    reader.feed_eof()
    writer = MagicMock()
    writer.drain = AsyncMock()
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(identity.asyncio, "open_connection", connect)
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", BASE)
    return connect, writer


@pytest.mark.parametrize("cluster", ["Healthcare", "arn:aws:ecs:us-east-1:123456789012:cluster/Healthcare"])
async def test_only_observed_whitelisted_fields_leave_the_metadata_reader(monkeypatch, cluster):
    """Preserve AWS field names and original strings while discarding all unrelated metadata."""
    fields = {**FIELDS, "Cluster": cluster}
    payload = {
        **fields,
        "Containers": [{"Environment": {"DB_PASSWORD": "private-value"}, "Labels": {"token": "private-value"}}],
        "TaskTags": {"secret": "private-value"},
        "Credentials": {"AccessKeyId": "private-value"},
        "Networks": [{"IPv4Addresses": ["private-value"]}],
    }
    connect, writer = metadata_response(monkeypatch, payload)
    monkeypatch.setenv("HTTP_PROXY", "http://private-value.invalid")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/private-value")
    observed = await identity.runtime_identity()
    assert observed == {"event": "ecs_runtime_identity", "status": "available", **fields}
    connect.assert_awaited_once_with("169.254.170.2", 80, limit=8192)
    request = writer.write.call_args.args[0].decode()
    assert request == (
        f"GET {BASE.removeprefix('http://169.254.170.2')}/task HTTP/1.0\r\n"
        "Host: 169.254.170.2\r\nConnection: close\r\n\r\n"
    )
    assert "private-value" not in json.dumps(observed) + request
    writer.close.assert_called_once()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/v4/abc",
        "http://localhost/v4/abc",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials",
        "http://169.254.170.2/v2/credentials/abc",
        "http://169.254.170.2/v4/abc/taskWithTags",
        "http://169.254.170.2:8080/v4/abc",
        "https://169.254.170.2/v4/abc",
        "http://user:private-value@169.254.170.2/v4/abc",
        "http://169.254.170.2.invalid/v4/abc",
        "http://169.254.170.2/v4/../credentials",
        "http://169.254.170.2/v4/%2e%2e",
        "http://169.254.170.2/v4/abc?token=private-value",
        "http://169.254.170.2/v4/abc#private-value",
        "http://169.254.170.2/v4/abc\r\nAuthorization: private-value",
    ],
)
async def test_invalid_endpoints_never_open_a_connection(monkeypatch, no_real_metadata, endpoint):
    """Reject credentials, redirects-by-URL, alternate ports and path/header injection before I/O."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", endpoint)
    observed = await identity.runtime_identity()
    assert observed == {
        "event": "ecs_runtime_identity",
        "status": "unavailable",
        "reason": "invalid_endpoint",
        "error_type": "ValueError",
    }
    no_real_metadata.assert_not_awaited()


async def test_non_ecs_has_safe_absence_without_network(no_real_metadata):
    """Local startup neither guesses task identity nor contacts a metadata endpoint."""
    assert await identity.runtime_identity() == {
        "event": "ecs_runtime_identity",
        "status": "unavailable",
        "reason": "not_ecs",
    }
    no_real_metadata.assert_not_awaited()


async def test_missing_or_invalid_identity_is_not_reconstructed(monkeypatch):
    """Keep valid partial fields but never build ARNs or coerce revision types."""
    metadata_response(
        monkeypatch,
        {"Cluster": "Healthcare", "Family": "bad\nprivate-value", "Revision": 17, "TaskID": TASK.rsplit("/", 1)[-1]},
    )
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("DEPLOYED_REVISION", "expected-answer")
    observed = await identity.runtime_identity()
    assert observed == {
        "event": "ecs_runtime_identity",
        "status": "partial",
        "reason": "incomplete_metadata",
        "Cluster": "Healthcare",
        "missing_fields": ["TaskARN", "Family", "Revision"],
    }


@pytest.mark.parametrize(
    ("header", "payload"),
    [
        (b"HTTP/1.1 302 Found\r\nLocation: http://private-value.invalid\r\n\r\n", FIELDS),
        (b"HTTP/1.1 500 Error\r\n\r\n", {"error": "private-value"}),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", b"private-value"),
        (b"HTTP/1.1 200 OK\r\n\r\n", b'{"password":"private-value"'),
        (b"HTTP/1.1 200 OK\r\n\r\n", ["private-value"]),
        (b"HTTP/1.1 200 OK\r\n\r\n", b"x" * (identity.MAX_METADATA_BYTES + 1)),
        (b"HTTP/1.1 200 OK\r\nX: " + b"x" * 8192 + b"\r\n\r\n", FIELDS),
    ],
)
async def test_bad_response_is_bounded_safe_absence(monkeypatch, header, payload):
    """Do not follow redirects, retain error bodies or allow unlimited metadata input."""
    connect, writer = metadata_response(monkeypatch, payload, header=header)
    observed = await identity.runtime_identity()
    assert observed["status"] == "unavailable"
    assert observed["reason"] == "metadata_unavailable"
    assert observed["error_type"]
    assert "private-value" not in json.dumps(observed)
    assert "TaskARN" not in observed
    connect.assert_awaited_once()
    writer.close.assert_called_once()


async def test_total_two_second_deadline_cancels_a_slow_body(monkeypatch):
    """A peer sending incomplete JSON cannot extend the overall startup deadline."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"HTTP/1.0 200 OK\r\n\r\n{")
    writer = MagicMock()
    writer.drain = AsyncMock()
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", BASE)
    monkeypatch.setattr(identity.asyncio, "open_connection", AsyncMock(return_value=(reader, writer)))
    started = asyncio.get_running_loop().time()
    observed = await identity.runtime_identity()
    elapsed = asyncio.get_running_loop().time() - started
    assert identity.METADATA_TIMEOUT_SECONDS == 2
    assert 1.8 <= elapsed < 3.0
    assert observed["error_type"] == "TimeoutError"
    assert observed["status"] == "unavailable"
    writer.close.assert_called_once()


async def test_transport_error_does_not_expose_message(monkeypatch, no_real_metadata):
    """Log only an exception class even if an underlying error contains sensitive text."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", BASE)
    no_real_metadata.side_effect = OSError("private-value")
    observed = await identity.runtime_identity()
    assert observed["error_type"] == "OSError"
    assert "private-value" not in json.dumps(observed)
