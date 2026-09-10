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
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
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
        fault_db_leak=False,
        fault_error_rate=0,
        fault_slow_query_ms=0,
        traffic_enabled=False,
    )
    database = container.database
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def instrument(app, settings):
        """Keep the real FastAPI tracing wrappers while preventing network export or global state."""
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)

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
            {"status": span.status.description, "events": [dict(event.attributes) for event in span.events]}
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
    """A real ORM failure must return 500 without escaping to ASGI, while r1 returns its connection."""
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
