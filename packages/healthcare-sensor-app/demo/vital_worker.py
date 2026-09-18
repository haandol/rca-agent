"""Local proof worker for a compiled source tree and an explicitly owned PostgreSQL schema."""

import argparse
import asyncio
import json
import logging
import re
import signal
import time
from dataclasses import asdict

from local_runner import read_dsn
from sqlalchemy.ext.asyncio import create_async_engine

from test_service.adapters.secondary.vital_repository.postgresql import PostgreSQLVitalRepository
from test_service.revision.manifest import source_manifest
from test_service.services.symptom_metrics import SymptomMetrics
from test_service.services.vital import VitalService


class Events(logging.Handler):
    def __init__(self):
        """Capture only safe receipt metadata, never bound values or exception prose."""
        super().__init__()
        self.events = []

    def emit(self, record):
        """Keep input/commit/error correlation without copying arbitrary logger fields."""
        if getattr(record, "event", None):
            keys = {
                "event",
                "observed_at",
                "sqlstate",
                "error_type",
                "count",
                "request_id",
                "operation",
                "completion_semantics",
                "event_schema_version",
                "input_contract",
                "input_contract_sha256",
                "reading_ref",
                "retry_delay_seconds",
                "sql_hash",
                "sql_hash_algorithm",
                "schema_name",
                "table_name",
                "column_names",
                "metric_namespace",
                "service_name",
                "attempt_metric",
                "failure_metric",
                "attempt_semantics",
                "failure_semantics",
                "cancellation_semantics",
                "success_evidence_event",
                "success_count_field",
                "success_semantics",
            }
            self.events.append({key: value for key, value in vars(record).items() if key in keys})


async def run(args):
    """Run actual selected INSERT code against the private schema; pending state survives this process exit."""
    if not re.fullmatch(r"vital_[a-f0-9]+", args.schema):
        raise ValueError("invalid proof schema")
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, current.cancel)
    engine = create_async_engine(
        read_dsn(args.env_file, args.expected_port),
        pool_size=5,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": args.schema, "application_name": "vital-proof-worker"}},
    )
    captured = Events()
    logger = logging.getLogger("test_service")
    logger.addHandler(captured)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    results = []
    try:
        metrics = SymptomMetrics("healthcare-sensor-app", flush_interval=30)
        frames = []
        metrics._emit = frames.append
        repository = PostgreSQLVitalRepository(engine, metrics)
        checkpoint_before = await repository.checkpoint()
        if args.run_seconds:
            service = VitalService(repository, metrics)
            running = asyncio.create_task(service.run(generate=args.generate))
            try:
                await asyncio.sleep(args.run_seconds)
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        deadline = time.monotonic() + args.drain_seconds
        for _ in range(args.steps):
            result = await repository.process_one(0)
            results.append(asdict(result))
        while args.drain_seconds and time.monotonic() < deadline:
            if (await repository.snapshot())["pending_count"] == 0:
                break
            results.append(asdict(await repository.process_one(0)))
            await asyncio.sleep(0.05)
        metrics.flush()
        return {
            "source": source_manifest(),
            "results": results,
            "events": captured.events,
            "snapshot": await repository.snapshot(),
            "checkpoint_before": checkpoint_before,
            "checkpoint_after": await repository.checkpoint(),
            "metric_frames": frames,
        }
    finally:
        logger.removeHandler(captured)
        await engine.dispose()
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--expected-port", type=int, required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--steps", type=int, default=0)
    parser.add_argument("--drain-seconds", type=float, default=0)
    parser.add_argument("--run-seconds", type=float, default=0)
    parser.add_argument("--generate", action="store_true")
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args())), default=str))
    except BaseException as error:
        print(json.dumps({"status": "FAIL", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
