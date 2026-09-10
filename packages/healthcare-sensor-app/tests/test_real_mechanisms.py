"""Contract tests complement the opt-in, real PostgreSQL proof runner."""

import asyncio
import copy
import importlib.util
import json
import logging
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_service.adapters.secondary import database_adapter
from test_service.adapters.secondary.sensor_repository.models import SensorReadingRow
from test_service.config import get_settings
from test_service.maintenance import MAX_HOLD_SECONDS, hold_lock
from test_service.ports.interfaces.database import DatabasePort
from test_service.revision.query import fetch_patient_rows
from test_service.revision.session import session_scope
from test_service.services.db_observability import current_operation, install_hooks, operation_context

PACKAGE = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_revision_is_immutable_and_default_is_stable(tmp_path):
    builder = load_file("revision_builder", PACKAGE / "demo/build_revision.py")
    fingerprints = set()
    for revision in ("r1", "r2", "r3"):
        destination = tmp_path / revision
        manifest = builder.compile_revision(revision, destination)
        fingerprints.add(manifest["fingerprint"])
        env = {**os.environ, "PYTHONPATH": str(destination), "SOURCE_REVISION": "r3", "DEPLOYED_REVISION": "r2"}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from test_service.revision.manifest import source_manifest; "
                "print(json.dumps(source_manifest()))",
            ],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        observed = json.loads(result.stdout)
        assert observed["revision"] == revision
        assert observed["verified"] is True
        assert observed["files"] == manifest["files"]
        with pytest.raises(ValueError, match="already compiled"):
            builder.compile_revision("r1", destination, in_place=True)
    assert len(fingerprints) == 3
    stable = tmp_path / "r1/test_service/revision/session.py"
    stable.write_text(stable.read_text() + "\n# unexpected mutation\n")
    result = subprocess.run(
        [sys.executable, "-c", "from test_service.revision.manifest import source_manifest; source_manifest()"],
        env={**os.environ, "PYTHONPATH": str(tmp_path / "r1")},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "differs from the build manifest" in result.stderr


def test_revisions_and_worker_share_frozen_source_even_if_workspace_changes(tmp_path, monkeypatch):
    builder = load_file("snapshot_builder", PACKAGE / "demo/build_revision.py")
    workspace = tmp_path / "workspace"
    files = {
        "src/test_service/revision/session.py": "# stable session\n",
        "src/test_service/revision/query.py": "# stable query\n",
        "demo/revisions/r2/query.py": "# original r2\n",
        "demo/revisions/r3/session.py": "# original r3\n",
        "demo/local_worker.py": "# original worker\n",
        "demo/build_revision.py": "# builder\n",
    }
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr(builder, "PACKAGE", workspace)
    snapshot = tmp_path / "snapshot"
    receipt = builder.capture_source_snapshot(snapshot)
    for relative in files:
        (workspace / relative).write_text("# concurrent edit\n")
    manifests = [
        builder.compile_revision(revision, tmp_path / revision, source_package=snapshot)
        for revision in ("r1", "r2", "r3")
    ]
    assert len({manifest["base_fingerprint"] for manifest in manifests}) == 1
    assert (tmp_path / "r1/test_service/revision/query.py").read_text() == "# stable query\n"
    assert (tmp_path / "r2/test_service/revision/query.py").read_text() == "# original r2\n"
    assert (tmp_path / "r3/test_service/revision/session.py").read_text() == "# original r3\n"
    assert (snapshot / "demo/local_worker.py").read_text() == "# original worker\n"
    assert datetime.fromisoformat(receipt["captured_at"]).utcoffset().total_seconds() == 0


async def test_operation_windows_are_captured_during_success_and_failure():
    worker = load_file("timed_proof_worker", PACKAGE / "demo/local_worker.py")

    async def action():
        return "result"

    async def failure():
        raise ValueError("test")

    for call, outcome in ((action, "ok"), (failure, "error")):
        before = datetime.now(UTC)
        record, _ = await worker.measured("timestamp_test", call)
        after = datetime.now(UTC)
        start = datetime.fromisoformat(record["started_at"])
        end = datetime.fromisoformat(record["completed_at"])
        assert record["outcome"] == outcome
        assert before <= start <= end <= after
        assert start.utcoffset().total_seconds() == end.utcoffset().total_seconds() == 0


def test_local_threshold_calibration_preserves_samples_and_rejects_overlap(tmp_path, monkeypatch):
    """Synthetic unit fixtures test calibration rules, not measured performance."""
    monkeypatch.syspath_prepend(str(PACKAGE / "demo"))
    runner = load_file("calibration_runner", PACKAGE / "demo/local_runner.py")
    phases = [
        {
            "case": "query",
            "phase": name,
            "query_input": {"patient_id": "unit", "limit": 120, "requests": 2},
            "measurement_started_at": "2026-09-10T00:00:00+00:00",
            "measurement_completed_at": "2026-09-10T00:00:01+00:00",
            "queries": [{"elapsed_ms": value} for value in values],
        }
        for name, values in (("normal", [10.0, 12.0]), ("fault", [80.0, 100.0]), ("restore", [11.0, 13.0]))
    ]
    original = copy.deepcopy(phases)
    result = runner.calibrate_local_query_threshold(phases)
    assert phases == original
    assert result["threshold_ms"] == 46.5
    assert result["relationship_verified"]
    assert result["aws_alarm_evaluated"] is False
    assert result["aws_threshold_calibrated"] is False
    assert result["phases"]["normal"]["below_threshold_count"] == 2
    assert result["phases"]["fault"]["above_threshold_count"] == 2
    assert result["phases"]["restore"]["below_threshold_count"] == 2
    phases[-1]["queries"][-1]["elapsed_ms"] = 81
    with pytest.raises(ValueError, match="No threshold separates"):
        runner.calibrate_local_query_threshold(phases)
    phases = copy.deepcopy(original)
    phases[-1]["query_input"]["limit"] = 119
    with pytest.raises(ValueError, match="Query inputs differ"):
        runner.calibrate_local_query_threshold(phases)


async def test_n_plus_one_issues_real_selects_with_identical_filtered_ordered_rows(db_engine):
    install_hooks(db_engine)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory.begin() as session:
        await session.execute(
            insert(SensorReadingRow),
            [
                {
                    "id": str(i),
                    "patient_id": "owned",
                    "reading_type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                    "timestamp": datetime(2026, 1, 1),
                    "is_abnormal": False,
                }
                for i in range(4)
            ],
        )
    query = (
        select(SensorReadingRow)
        .where(SensorReadingRow.patient_id == "owned")
        .order_by(SensorReadingRow.timestamp.desc(), SensorReadingRow.id.desc())
        .limit(3)
    )
    overlay = load_file("revision_r2_query", PACKAGE / "demo/revisions/r2/query.py")
    results = []
    for fetch, count in ((fetch_patient_rows, 1), (overlay.fetch_patient_rows, 4)):
        with operation_context("patient_vitals"):
            async with factory() as session:
                rows = await fetch(session, query)
                results.append([(row.id, row.value, row.timestamp) for row in rows])
            assert current_operation().sql_count == count
    assert results[0] == results[1]
    assert [row[0] for row in results[0]] == ["3", "2", "1"]


async def test_sql_observation_never_records_parameters_or_inline_literals(db_engine, caplog):
    install_hooks(db_engine)
    async with db_engine.connect() as conn:
        with caplog.at_level(logging.INFO):
            with operation_context("outer"):
                outer = current_operation()
                with operation_context("inner"):
                    assert current_operation() is outer
                    await conn.execute(text("SELECT :secret"), {"secret": "sensitive-bound-value"})
                    await conn.execute(text("SELECT 'sensitive-inline-value'"))
                assert outer.sql_count == 2
                assert outer.sql_time_ms > 0
    assert current_operation() is None
    serialized = json.dumps([record.__dict__ for record in caplog.records], default=str)
    assert "sensitive-bound-value" not in serialized
    assert "sensitive-inline-value" not in serialized
    assert len([r for r in caplog.records if r.getMessage() == "db_operation"]) == 1


async def test_context_bridge_forwards_consumer_exception_and_closes_generator():
    received = []

    async def legacy_generator():
        try:
            yield "session"
        except ValueError:
            received.append("rollback")
            raise
        finally:
            received.append("close")

    port = SimpleNamespace(session=legacy_generator, leaky_session=legacy_generator)
    with pytest.raises(ValueError, match="consumer"):
        async with DatabasePort.session_context(port):
            raise ValueError("consumer")
    assert received == ["rollback", "close"]


async def test_maintenance_hold_bound_is_finite_and_accommodates_analysis_budget(monkeypatch):
    assert MAX_HOLD_SECONDS == 7200
    connect = AsyncMock()
    monkeypatch.setattr("test_service.maintenance.asyncpg.connect", connect)
    for hold_seconds in (0, -1, 7201, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="hold-seconds"):
            await hold_lock(
                "postgresql+asyncpg://unused:unused@localhost/unused",
                run_id="test",
                hold_seconds=hold_seconds,
                stop_event=asyncio.Event(),
            )
    connect.assert_not_called()


async def test_adapter_disposal_closes_only_its_owned_legacy_sessions(monkeypatch):
    engines = [MagicMock(dispose=AsyncMock()), MagicMock(dispose=AsyncMock())]
    monkeypatch.setattr(database_adapter, "create_async_engine", lambda *a, **kw: engines.pop())
    sessions = [MagicMock(close=AsyncMock()), MagicMock(close=AsyncMock())]
    monkeypatch.setattr(database_adapter, "async_sessionmaker", lambda *a, **kw: lambda: sessions.pop())
    settings = replace(get_settings(), fault_db_leak=True, db_observability_enabled=False)
    first = database_adapter.SqlAlchemyDatabaseAdapter(settings)
    second = database_adapter.SqlAlchemyDatabaseAdapter(settings)
    async with first.session_context(legacy_leak=True) as owned_first:
        pass
    async with second.session_context(legacy_leak=True) as owned_second:
        pass
    await first.dispose()
    owned_first.close.assert_awaited_once()
    owned_second.close.assert_not_awaited()
    assert database_adapter._leaked_connections == [owned_second]
    await second.dispose()
    assert database_adapter._leaked_connections == []


async def test_normal_scope_returns_transaction_on_cancellation(db_engine):
    owned = set()
    factory = async_sessionmaker(db_engine)
    ready = asyncio.Event()

    async def consumer():
        async with session_scope(factory, owned) as session:
            await session.execute(text("SELECT 1"))
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(consumer())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not owned
    async with factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


def test_local_runner_refuses_defaults_and_unowned_database(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE / "demo"))
    runner = load_file("proof_runner", PACKAGE / "demo/local_runner.py")
    env_file = tmp_path / "database.env"
    for dsn in (
        "postgresql+asyncpg://u:p@localhost:5432/rca_demo",
        "postgresql+asyncpg://u:p@127.0.0.1:5432/rca_demo",
        "postgresql+asyncpg://u:p@127.0.0.1:32768/another_project",
        "postgresql+asyncpg://u:p@external.example:32768/rca_demo",
    ):
        env_file.write_text("DATABASE_URL=" + dsn)
        with pytest.raises(ValueError, match="caller-owned"):
            runner.read_dsn(env_file, 32768)
    env_file.write_text("DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:32768/rca_demo")
    assert runner.read_dsn(env_file, 32768)


@pytest.mark.skipif(not os.environ.get("HEALTHCARE_PROOF_ENV_FILE"), reason="Requires caller-owned PostgreSQL")
def test_real_postgresql_all_mechanisms(tmp_path):
    """No mocked engine: separate compiled processes use an explicit local DSN."""
    output = tmp_path / "evidence"
    result = subprocess.run(
        [
            sys.executable,
            str(PACKAGE / "demo/local_runner.py"),
            "--env-file",
            os.environ["HEALTHCARE_PROOF_ENV_FILE"],
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["checks"]["all_four_mechanisms"]
    assert evidence["schema_removed"]
