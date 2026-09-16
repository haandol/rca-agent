"""Exercise F1 through the production app boundary without changing revision mechanisms."""

import asyncio
import importlib
import json
import logging
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import APIRouter, HTTPException
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pythonjsonlogger.json import JsonFormatter
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from test_service import telemetry
from test_service.adapters.secondary import database_adapter
from test_service.adapters.secondary.sensor_repository.models import Base
from test_service.config import get_settings
from test_service.di.app_container import AppContainer
from test_service.middleware.logging import LoggingMiddleware

CANARIES = ("CANARY_SQL_PARAMETER", "CANARY_DRIVER_DETAIL", "CANARY_CREDENTIAL")
READING = {
    "patient_id": CANARIES[0],
    "reading_type": "heart_rate",
    "value": 72,
    "unit": "bpm",
}


@pytest.fixture
async def http_boundary(tmp_path, monkeypatch):
    """Use production routing, session cleanup and tracing with a local DB and no exporters."""
    with (
        patch.object(telemetry, "setup_logging"),
        patch.object(telemetry, "setup_telemetry"),
        patch.object(AppContainer, "create_router", return_value=APIRouter()),
    ):
        main = importlib.import_module("test_service.main")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'boundary.db'}", hide_parameters=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database_adapter, "create_async_engine", lambda *args, **kwargs: engine)
    container = AppContainer()
    container._settings = replace(
        get_settings(),
        db_observability_enabled=True,
        traffic_enabled=False,
    )
    database = container.database
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def instrument(app, settings):
        """Keep the real FastAPI tracing wrappers while preventing network export or global state."""
        telemetry.instrument_http(app, provider)

    monkeypatch.setattr(main, "container", container)
    monkeypatch.setattr(main, "setup_logging", lambda settings: None)
    monkeypatch.setattr(main, "setup_telemetry", instrument)
    app = main.create_app()
    try:
        yield SimpleNamespace(app=app, database=database, metrics=container.symptom_metrics, exporter=exporter)
    finally:
        await database.dispose()
        provider.shutdown()


def assert_safe_records(caplog, exporter):
    """Inspect logs and trace errors so hidden exception objects fail, independent of URL tracing."""
    formatter = JsonFormatter()
    serialized = "\n".join(formatter.format(record) for record in caplog.records)
    serialized += json.dumps([record.__dict__ for record in caplog.records], default=str)
    serialized += json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes),
                "status": span.status.description,
                "events": [dict(event.attributes) for event in span.events],
            }
            for span in exporter.get_finished_spans()
        ]
    )
    for canary in CANARIES:
        assert canary not in serialized
    assert "INSERT INTO sensor_readings" not in serialized
    assert "SQL parameters hidden" not in serialized
    failures = [record for record in caplog.records if record.getMessage() == "Request failed"]
    assert len(failures) == 1
    assert failures[0].exc_info is None
    assert failures[0].stack_info is None
    return failures[0]


async def test_driver_detail_is_contained_after_session_return_and_failure_accounting(
    http_boundary, monkeypatch, caplog, capsys
):
    """A real ORM failure must return 500 without escaping to ASGI, while the session scope returns its connection."""
    caplog.set_level(logging.WARNING, logger="httpx")
    caplog.set_level(logging.INFO)
    database = http_boundary.database
    original_execute = database.engine.sync_engine.dialect.do_execute

    def failing_execute(cursor, statement, parameters, context=None):
        """Fail an actual insert with driver DETAIL, retaining SQLAlchemy's normal error wrapping."""
        if statement.startswith("INSERT INTO sensor_readings"):
            raise sqlite3.IntegrityError(f"DETAIL: failing row contains {' '.join(CANARIES)}")
        return original_execute(cursor, statement, parameters, context)

    monkeypatch.setattr(database.engine.sync_engine.dialect, "do_execute", failing_execute)
    transport = ASGITransport(app=http_boundary.app, raise_app_exceptions=True)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/sensors/data?token={CANARIES[2]}",
            json={"readings": [READING]},
            headers={"Authorization": f"Bearer {CANARIES[2]}"},
        )
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}
        assert not database._owned_sessions
        assert database.checked_out_connections() == 0
        operation = next(record for record in caplog.records if record.getMessage() == "db_operation")
        assert operation.outcome == "error"
        assert operation.sql_count == 1
        assert operation.checkouts == operation.checkins == 1
        assert operation.request_id
        http_boundary.metrics.flush()
        metrics = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert metrics["VitalIngestStarted"] == metrics["VitalIngestAttempts"] == metrics["VitalIngestFailures"] == 1
        assert metrics["VitalIngestInFlight"] == 0
        failure = assert_safe_records(caplog, http_boundary.exporter)
        assert failure.error_type == "IntegrityError"
        assert failure.method == "POST"
        assert failure.path == "/sensors/data"
        assert failure.status_code == 500
        assert failure.elapsed_ms >= 0
        spans = http_boundary.exporter.get_finished_spans()
        assert spans
        assert any(span.attributes.get("http.status_code") == 500 for span in spans)

        # The same production repository/session path remains usable after the failure.
        monkeypatch.setattr(database.engine.sync_engine.dialect, "do_execute", original_execute)
        response = await client.post("/sensors/data", json={"readings": [READING]})
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert database.checked_out_connections() == 0
        assert not database._owned_sessions
        http_boundary.metrics.flush()
        metrics = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert metrics["VitalIngestStarted"] == metrics["VitalIngestAttempts"] == 1
        assert metrics["VitalIngestFailures"] == metrics["VitalIngestInFlight"] == 0


@pytest.mark.parametrize("grouped", [False, True])
async def test_wrapped_driver_error_never_reaches_server_logging(http_boundary, caplog, grouped):
    """Strict ASGI transport exposes rethrows; grouped/chained driver details must stay inside."""
    caplog.set_level(logging.WARNING, logger="httpx")
    caplog.set_level(logging.INFO)

    async def failure(item: str):
        """Supply the review's DBAPIError canary through a real route, including exception chaining."""
        error = DBAPIError(
            "INSERT INTO sensor_readings VALUES ($1)",
            {"patient_id": CANARIES[0]},
            Exception(f"DETAIL: {CANARIES[1]} password={CANARIES[2]}"),
            hide_parameters=True,
        )
        if grouped:
            raise ExceptionGroup("driver failures", [error])
        raise error

    http_boundary.app.add_api_route("/failure/{item}", failure)
    async with AsyncClient(
        transport=ASGITransport(app=http_boundary.app, raise_app_exceptions=True), base_url="http://test"
    ) as client:
        response = await client.get(f"/failure/{CANARIES[0]}")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    record = assert_safe_records(caplog, http_boundary.exporter)
    assert record.path == "/failure/{item}"
    assert record.error_type == ("ExceptionGroup" if grouped else "DBAPIError")


async def test_ordinary_http_errors_keep_status_detail_and_headers(http_boundary):
    """Handled errors and validation remain ordinary responses rather than generic server failures."""

    async def denied():
        """Provide an explicit HTTP error so the exception middleware's existing contract is checked."""
        raise HTTPException(429, "Try later", headers={"Retry-After": "3"})

    http_boundary.app.add_api_route("/denied", denied)
    async with AsyncClient(
        transport=ASGITransport(app=http_boundary.app, raise_app_exceptions=True), base_url="http://test"
    ) as client:
        response = await client.get("/denied")
        assert response.status_code == 429
        assert response.json() == {"detail": "Try later"}
        assert response.headers["Retry-After"] == "3"
        assert (await client.get("/missing")).status_code == 404
        assert (await client.post("/sensors/data", json={})).status_code == 422
        assert (await client.get("/sensors/data")).status_code == 405


@pytest.mark.parametrize("background", [False, True])
async def test_late_response_failure_exposes_only_fresh_safe_exception(http_boundary, caplog, background):
    """Headers cannot be replaced; late failure must abort without retaining the sensitive cause."""
    caplog.set_level(logging.INFO)

    async def fail_late():
        """Raise after response start to catch leaks missed by dispatch-only middleware."""
        raise RuntimeError(" ".join(CANARIES))

    async def chunks():
        """Start streaming before failing so the boundary must preserve the already emitted status."""
        yield b"safe chunk"
        await fail_late()

    async def endpoint():
        """Exercise both framework response lifetimes through the real app middleware stack."""
        if background:
            return JSONResponse({"ok": True}, background=BackgroundTask(fail_late))
        return StreamingResponse(chunks())

    http_boundary.app.add_api_route("/late", endpoint)
    async with AsyncClient(
        transport=ASGITransport(app=http_boundary.app, raise_app_exceptions=True), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError, match="HTTP response failed after headers were sent") as caught:
            await client.get("/late")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    record = assert_safe_records(caplog, http_boundary.exporter)
    assert record.status_code == 200


async def test_http_cancellation_is_not_converted_to_500(http_boundary, caplog):
    """Keep task cancellation visible to the caller instead of counting it as an HTTP failure."""
    caplog.set_level(logging.INFO)
    entered = asyncio.Event()

    async def cancelled():
        """Wait for server task cancellation without manufacturing an endpoint error."""
        entered.set()
        await asyncio.Future()

    http_boundary.app.add_api_route("/cancelled", cancelled)
    async with AsyncClient(
        transport=ASGITransport(app=http_boundary.app, raise_app_exceptions=True), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.get("/cancelled"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert not [record for record in caplog.records if record.getMessage() == "Request failed"]


async def test_failed_500_send_does_not_chain_the_original_driver_exception():
    """A disconnected client during fallback sending must not restore the sensitive exception chain."""

    async def failing_app(scope, receive, send):
        """Raise a sensitive error before any headers reach the boundary."""
        raise DBAPIError("secret SQL", {}, Exception(" ".join(CANARIES)), hide_parameters=True)

    async def receive():
        """Supply an empty HTTP body without an external client."""
        return {"type": "http.request", "body": b""}

    async def disconnected_send(message):
        """Model a transport failure while the generic 500 is being sent."""
        raise BrokenPipeError("client disconnected")

    with pytest.raises(BrokenPipeError, match="client disconnected") as caught:
        await LoggingMiddleware(failing_app)({"type": "http", "method": "GET"}, receive, disconnected_send)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("field", ["timestamp", "value", "reading_type", "missing"])
async def test_validation_errors_expose_only_safe_detail(http_boundary, caplog, field):
    """Reject invalid sensor input without echoing body, context or custom text into any evidence."""
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.WARNING, logger="httpx")
    reading = {**READING, "unit": CANARIES[2]}
    if field == "missing":
        del reading["value"]
    else:
        reading[field] = CANARIES[1]
    async with AsyncClient(transport=ASGITransport(app=http_boundary.app), base_url="http://test") as client:
        response = await client.post("/sensors/data", json={"readings": [reading]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "readings", 0, "value" if field == "missing" else field]
    assert all(set(error) == {"type", "loc", "msg"} for error in detail)
    evidence = response.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    evidence += str(
        [(dict(s.attributes), s.events, s.status.description) for s in http_boundary.exporter.get_finished_spans()]
    )
    assert http_boundary.exporter.get_finished_spans()
    assert all(canary not in evidence for canary in CANARIES)


async def test_custom_validation_context_message_and_location_are_not_echoed(http_boundary, caplog):
    """A custom validator cannot smuggle patient values through type, location, message or context."""
    from fastapi.exceptions import RequestValidationError

    async def invalid():
        """Model an application validator whose error metadata contains sensitive values."""
        raise RequestValidationError(
            [
                {
                    "type": CANARIES[0],
                    "loc": ("body", CANARIES[1]),
                    "msg": CANARIES[2],
                    "input": CANARIES[0],
                    "ctx": {"error": ValueError(CANARIES[1])},
                }
            ],
            body=CANARIES[2],
        )

    http_boundary.app.add_api_route("/invalid", invalid)
    async with AsyncClient(transport=ASGITransport(app=http_boundary.app), base_url="http://test") as client:
        response = await client.get("/invalid")
    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "value_error", "loc": ["body", "[REDACTED]"], "msg": "Invalid input"}]
    }
    evidence = response.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    evidence += str(
        [(dict(s.attributes), s.events, s.status.description) for s in http_boundary.exporter.get_finished_spans()]
    )
    assert all(canary not in evidence for canary in CANARIES)


def test_real_uvicorn_process_never_logs_raw_patient_urls(tmp_path):
    """Use Docker's server arguments over real sockets, retaining safe status logs on all paths."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx

    package = Path(__file__).resolve().parents[1]
    command = json.loads(
        next(
            line[4:] for line in (package / "Dockerfile").read_text().splitlines() if line.startswith('CMD ["uvicorn"')
        )
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        # Only external exporters and the DB-dependent service method are replaced.
        # Production routing, exception handling, logging and Uvicorn stay real.
        (tmp_path / "privacy_server.py").write_text("""
from test_service import telemetry
telemetry.setup_telemetry = lambda app, settings: None
from test_service.main import app, container
async def vitals(*args, **kwargs):
    if kwargs.get("limit") == 1:
        raise RuntimeError("CANARY_DRIVER_DETAIL")
    return []
container.sensor_service.get_patient_vitals = vitals
""")
        command[1] = "privacy_server:app"
        # Inherit a bound socket to avoid a free-port race; all other server options
        # come from the deployable Docker command, including access-log policy.
        command += ["--fd", str(listener.fileno()), "--lifespan", "off"]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(package / "src")])}
        with (tmp_path / "server.log").open("w+") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", *command],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                pass_fds=(listener.fileno(),),
            )
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1) as client:
                    for _ in range(100):
                        if process.poll() is not None:
                            pytest.fail("Uvicorn exited before readiness")
                        try:
                            client.get("/missing")
                            break
                        except httpx.TransportError:
                            time.sleep(0.05)
                    else:
                        pytest.fail("Uvicorn did not become ready")
                    for path, status in [
                        (f"/patients/{CANARIES[0]}/vitals?limit=2", 200),
                        (f"/patients/{CANARIES[0]}/vitals?limit=1", 500),
                        (f"/patients/{CANARIES[0]}/vitals?limit={CANARIES[1]}", 422),
                        (f"/missing/{CANARIES[0]}?unused=1", 404),
                    ]:
                        response = client.get(f"{path}&token={CANARIES[2]}")
                        assert response.status_code == status
                        assert all(canary not in response.text for canary in CANARIES)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.returncode in (0, -signal.SIGTERM)
            output.seek(0)
            logs = output.read()
        assert "Finished server process" in logs
        assert all(canary not in logs for canary in CANARIES)
        records = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
        requests = [r for r in records if r["message"] in {"Handled request", "Request failed"}]
        assert {200, 500, 422, 404} <= {r["status_code"] for r in requests}
        assert all(r["path"] in {"/patients/{patient_id}/vitals", "<unmatched>"} for r in requests)
