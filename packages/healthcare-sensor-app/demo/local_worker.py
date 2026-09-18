"""Run one compiled revision against the owned schema using real HTTP and PostgreSQL."""

import argparse
import asyncio
import importlib
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pythonjsonlogger.json import JsonFormatter
from sqlalchemy import text
from sqlalchemy.engine import make_url

from test_service import telemetry
from test_service.adapters.secondary import database_adapter
from test_service.adapters.secondary.sensor_repository.models import Base
from test_service.config import get_settings
from test_service.di.app_container import AppContainer
from test_service.revision.manifest import source_manifest

CANARIES = ("CANARY_PATIENT_913", "CANARY_UNIT_913", "CANARY_AUTH_913")
READING = {
    "patient_id": CANARIES[0],
    "unit": CANARIES[1],
    "value": 120,
    "reading_type": "heart_rate",
    "timestamp": "2026-09-15T00:00:00Z",
}


class Capture(logging.Handler):
    """Serialize every application log for private canary checking."""

    def __init__(self):
        """Keep formatted records private until the complete privacy assertion passes."""
        super().__init__()
        self.events = []
        self.setFormatter(JsonFormatter())

    def emit(self, record):
        """Capture JSON without printing parameter-bearing exception objects."""
        self.events.append(json.loads(self.format(record)))


async def run(args):
    """Exercise same-input writes and independent reads, retaining only sanitized proof."""
    if not re.fullmatch(r"proof_local_[0-9a-f]{16}", args.schema):
        raise ValueError("Invalid owned schema")
    url = make_url(os.environ["DATABASE_URL"])
    if url.host != "127.0.0.1" or url.port != args.expected_port or url.port == 5432 or url.database != "rca_demo":
        raise ValueError("Worker requires the caller-owned local PostgreSQL")
    capture = Capture()
    logging.getLogger().handlers = [capture]
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    real_create = database_adapter.create_async_engine

    def create_engine(*positional, **kwargs):
        """Set only this proof process's search path without changing shared service settings."""
        kwargs["connect_args"]["server_settings"]["search_path"] = args.schema
        return real_create(*positional, **kwargs)

    container = AppContainer()
    container._settings = replace(get_settings(), traffic_enabled=False, db_observability_enabled=True)
    lifecycle = AsyncExitStack()
    lifecycle.enter_context(patch.object(database_adapter, "create_async_engine", create_engine))
    database = container.database
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    proof = {"phase": args.phase, "source": source_manifest()}
    real_dispose = database.dispose

    async def checked_dispose():
        """Verify the actual pool only after owned background work drains, before disposal hides pool state."""
        proof["checked_out"] = database.checked_out_connections()
        assert proof["checked_out"] == 0 and not database._owned_sessions
        await real_dispose()

    lifecycle.enter_context(patch.object(database, "dispose", checked_dispose))
    try:
        if args.case == "setup":
            async with database.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            proof["schema_snapshot"] = await database.schema_snapshot()
            return proof
        before_schema = await database.schema_snapshot()
        assert "timestamp" in before_schema["column_names"] and "sampled_at" not in before_schema["column_names"]
        with (
            patch.object(telemetry, "setup_logging"),
            patch.object(telemetry, "setup_telemetry"),
            patch.object(AppContainer, "create_router", return_value=APIRouter()),
        ):
            main = importlib.import_module("test_service.main")
        with (
            patch.object(main, "container", container),
            patch.object(main, "setup_logging"),
            patch.object(
                main,
                "setup_telemetry",
                lambda app, _: telemetry.instrument_http(app, provider),
            ),
        ):
            app = main.create_app()
        emitted = []
        container.symptom_metrics._emit = emitted.append
        lifecycle.enter_context(patch.object(main, "container", container))
        lifecycle.enter_context(patch.object(main, "containers", [container]))
        await lifecycle.enter_async_context(main.lifespan(app))

        async def row_ids():
            """Compare persisted identities privately; patient fields never enter evidence."""
            async with database.engine.connect() as conn:
                return set((await conn.execute(text("SELECT id FROM sensor_readings"))).scalars())

        before = await row_ids()
        statuses = []
        responses = []
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=True), base_url="http://proof"
        ) as client:
            for _ in range(3):
                response = await client.post(
                    "/sensors/data", json={"readings": [READING, READING]}, headers={"Authorization": CANARIES[2]}
                )
                statuses.append(response.status_code)
                if response.status_code != 200:
                    responses.append(response.text)
                assert not database._owned_sessions
            read = await client.get(f"/patients/{CANARIES[0]}/vitals")
            alerts = await client.get("/alerts")
            health = await client.get("/healthz")
        after = await row_ids()
        after_schema = await database.schema_snapshot()
        assert before_schema["column_names"] == after_schema["column_names"]
        assert before_schema["schema_name"] == after_schema["schema_name"]
        container.symptom_metrics.flush()
        spans = [
            {
                "name": s.name,
                "attributes": dict(s.attributes),
                "status": s.status.description,
                "events": [dict(e.attributes) for e in s.events],
            }
            for s in exporter.get_finished_spans()
        ]
        serialized = json.dumps([capture.events, spans, responses], default=str)
        secrets = [*CANARIES, url.password, os.environ["DATABASE_URL"]]
        assert all(secret not in serialized for secret in secrets if secret)
        assert "INSERT INTO sensor_readings" not in serialized
        proof.update(
            {
                "schema_snapshot": after_schema,
                "write_statuses": statuses,
                "rows_before": len(before),
                "rows_after": len(after),
                "existing_rows_preserved": before <= after,
                "read_status": read.status_code,
                "alerts_status": alerts.status_code,
                "read_count": len(read.json()),
                "alert_count": len(alerts.json()),
                "health": health.json(),
                "checked_out_during_background": database.checked_out_connections(),
                "canaries_absent": True,
                "events": capture.events,
                "metrics": emitted[-1],
                "trace_count": len(spans),
            }
        )
        return proof
    finally:
        await lifecycle.aclose()
        await database.dispose()
        proof["owned_sessions_after_dispose"] = len(database._owned_sessions)
        provider.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("setup", "insert"), required=True)
    parser.add_argument("--phase", choices=("normal", "fault", "restore"), required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-port", type=int, default=15439)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        import traceback

        frames = traceback.extract_tb(exc.__traceback__)
        args.output.write_text(
            json.dumps({"failure_type": type(exc).__name__, "failure_line": frames[-1].lineno if frames else None})
        )
        raise SystemExit(1) from None
    args.output.write_text(json.dumps(result, indent=2, default=str))
