"""Real PostgreSQL transactions, competing connections, process replacement and owned-container restart."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from test_service.adapters.secondary.sensor_repository.models import SensorReadingRow
from test_service.adapters.secondary.vital_repository.postgresql import (
    CAPACITY,
    COORDINATOR,
    IDENTITY,
    INBOX,
    PostgreSQLVitalRepository,
    initialize_vital_schema,
)
from test_service.ports.dto.vital import EventConflictError, InboxFullError, normalize_vital_event
from test_service.services.symptom_metrics import SymptomMetrics
from tests.test_write_revision import PACKAGE, builder_module

pytestmark = pytest.mark.skipif(
    not os.environ.get("VITAL_PG_PROOF_CONFIG"), reason="Requires task-owned native PostgreSQL proof configuration"
)


class SecretDSN(str):
    def __repr__(self):
        """Keep pytest fixture diagnostics from revealing the task-owned database credential."""
        return "<REDACTED_DATABASE_URL>"


@pytest.fixture(scope="session")
def proof_config():
    """Use only an explicitly named task-owned container/port, never an existing application database."""
    config = json.loads(Path(os.environ["VITAL_PG_PROOF_CONFIG"]).read_text())
    assert config["container"].startswith("rca-vital-native-") and config["port"] not in (5432, 15439)
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("vital_proof_reader", PACKAGE / "demo/local_runner.py")
    module = module_from_spec(spec)
    sys.path.insert(0, str(PACKAGE / "demo"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    config["dsn"] = SecretDSN(module.read_dsn(Path(config["env_file"]), config["port"]))
    config["reap_phase_group"] = module.reap_phase_group
    return config


@pytest.fixture
async def native(proof_config):
    """Create and drop only this test's private schema while retaining database/container state between phases."""
    schema = "vital_" + uuid.uuid4().hex
    admin = create_async_engine(proof_config["dsn"], poolclass=NullPool, hide_parameters=True)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        proof_config["dsn"],
        pool_size=5,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema, "application_name": "vital-native-test"}},
    )
    try:
        await initialize_vital_schema(engine)
        yield engine, PostgreSQLVitalRepository(engine), schema
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def event(number=0, *, version=2, value=72.5):
    """Make explicit synthetic subject/sensor inputs; tests report only counts and hashes."""
    payload = {
        "event_id": f"event-{number}",
        "sensor_id": "synthetic-sensor",
        "patient_id": "P-001",
        "schema_version": version,
        "reading_type": "heart_rate",
        "value": value,
        "unit": "bpm",
    }
    payload["timestamp" if version == 1 else "sampled_at"] = (
        datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=number)
    ).isoformat()
    return normalize_vital_event(payload)


async def count(engine, table):
    """Read actual persisted rows rather than infer completion from the worker result."""
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


def record(config, name, result):
    """Persist non-sensitive native proof results; retain evidence independently of fixture cleanup."""
    Path(config["artifact_dir"], name + ".json").write_text(json.dumps(result, indent=2, default=str) + "\n")


async def test_native_versions_identity_conflicts_and_atomic_completion(native, proof_config):
    engine, repo, _ = native
    for version in (1, 2):
        current = event(version, version=version)
        assert (await repo.admit(current)).state == "PENDING"
        assert (await repo.process_one(0)).state == "COMMITTED"
        assert (await repo.admit(current)).duplicate
        with pytest.raises(EventConflictError):
            await repo.admit(event(version, version=version, value=74))
    checkpoint = await repo.checkpoint()
    status = await repo.cohort(checkpoint["epoch"], 0, 2)
    assert status["complete"] and status["matched_measurements"] == 2
    assert await count(engine, INBOX) == 0 and await count(engine, SensorReadingRow.__table__) == 2
    snapshot = await repo.snapshot()
    assert snapshot["accepted_total"] == snapshot["committed_total"] == 2
    record(proof_config, "versions-and-identity", {"status": "PASS", "cohort": status})


async def test_native_parallel_same_id_and_workers_insert_once(native, proof_config):
    engine, repo, _ = native
    receipts = await asyncio.gather(*(repo.admit(event()) for _ in range(12)))
    assert sum(not r.duplicate for r in receipts) == 1
    outcomes = await asyncio.gather(*(repo.process_one(i % 2) for i in range(12)))
    assert sum(r.state == "COMMITTED" for r in outcomes) == 1
    assert await count(engine, SensorReadingRow.__table__) == 1 and await count(engine, INBOX) == 0
    record(proof_config, "parallel-id-workers", {"status": "PASS", "admitted_once": True, "inserted_once": True})


async def test_native_commit_ack_lost_has_single_effect_without_confirmed_metrics(native, proof_config):
    engine, repo, _ = native
    await repo.admit(event())
    metrics = SymptomMetrics("healthcare-sensor-app", flush_interval=1000)

    class LostAck:
        sync_engine = engine.sync_engine

        @asynccontextmanager
        async def begin(self):
            """Commit in real PostgreSQL, then lose only the acknowledgement boundary."""
            async with engine.begin() as conn:
                yield conn
            raise ConnectionError("simulated commit acknowledgement loss")

    unknown = PostgreSQLVitalRepository(LostAck(), metrics)
    with pytest.raises(ConnectionError):
        await unknown.process_one(0)
    assert metrics._attempts == 0 and metrics._failures == 0 and metrics._in_flight == 0
    assert metrics._measurement_sql_started == 1
    assert (await repo.admit(event())).state == "COMPLETED"
    assert (await repo.process_one(0)).state == "NO_WORK"
    assert await count(engine, SensorReadingRow.__table__) == 1
    snapshot = await repo.snapshot()
    assert snapshot["committed_total"] == 1 and snapshot["pending_count"] == 0
    record(
        proof_config,
        "lost-commit-ack",
        {"status": "PASS", "actual_sql_started": 1, "confirmed_metrics": 0, "unique_rows": 1, "durable_commits": 1},
    )


async def test_native_generation_continues_while_measurement_insert_is_blocked(native, proof_config):
    engine, repo, schema = native
    await repo.admit(event())
    async with engine.begin() as setup:
        await setup.execute(
            text(
                "CREATE FUNCTION pause_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM pg_advisory_xact_lock(hashtext(current_schema()),918777); RETURN NEW; END $$"
            )
        )
        await setup.execute(
            text(
                "CREATE TRIGGER pause_insert BEFORE INSERT ON sensor_readings "
                "FOR EACH ROW EXECUTE FUNCTION pause_insert()"
            )
        )
    async with engine.begin() as blocker:
        await blocker.execute(text("SELECT pg_advisory_xact_lock(hashtext(current_schema()),918777)"))
        worker = asyncio.create_task(repo.process_one(0))
        try:
            blocked = False
            for _ in range(40):
                async with engine.connect() as observer:
                    blocked = (
                        await observer.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE application_name='vital-native-test' "
                                "AND wait_event_type='Lock' AND query LIKE 'INSERT INTO sensor_readings%'"
                            )
                        )
                    ).scalar_one() > 0
                if blocked:
                    break
                await asyncio.sleep(0.05)
            assert blocked
            before = (await repo.snapshot())["generated_total"]
            await asyncio.wait_for(repo.generate_once(), timeout=1)
            assert (await repo.snapshot())["generated_total"] == before + 1
            assert not worker.done()
        finally:
            # Exiting this transaction releases only the deliberate test lock.
            pass
    assert (await worker).state == "COMMITTED"
    record(
        proof_config,
        "producer-independent-from-blocked-insert",
        {"status": "PASS", "actual_pg_lock_wait": True, "generated_during_wait": 1},
    )


async def test_native_global_one_hz_and_no_downtime_replay(native, proof_config):
    engine, repo, _ = native
    deadline = time.monotonic() + 2.2
    while time.monotonic() < deadline:
        await asyncio.gather(*(repo.generate_once() for _ in range(5)))
        await asyncio.sleep(0.05)
    async with engine.connect() as conn:
        groups = (
            await conn.execute(
                text("SELECT date_trunc('second', created_at),count(*) FROM vital_event_identity GROUP BY 1 ORDER BY 1")
            )
        ).all()
    assert len(groups) >= 2 and all(row[1] == 1 for row in groups)
    before = await repo.snapshot()
    await asyncio.sleep(2.1)
    await repo.generate_once()
    after = await repo.snapshot()
    assert after["generated_total"] == before["generated_total"] + 1
    record(
        proof_config,
        "global-cadence",
        {"status": "PASS", "observed_slots": len(groups), "max_events_per_second": 1, "after_downtime_new_events": 1},
    )


async def test_native_real_86400_capacity_duplicates_and_release(native, proof_config):
    engine, repo, _ = native
    # Seed real identity+inbox rows, not a forged counter; boundary arrivals use the production admission API.
    async with engine.begin() as conn:
        now = (await conn.execute(select(func.clock_timestamp()))).scalar_one()
        for offset in range(0, CAPACITY - 2, 1000):
            events = [event(i) for i in range(offset, min(offset + 1000, CAPACITY - 2))]
            await conn.execute(
                insert(IDENTITY),
                [
                    {
                        "event_id": e.event_id,
                        "payload_sha256": e.digest,
                        "sensor_id": e.sensor_id,
                        "schema_version": e.schema_version,
                        "reading_id": str(uuid.uuid4()),
                        "measurement_id": None,
                        "admission_sequence": i + 1,
                        "created_at": now,
                        "completed_at": None,
                    }
                    for i, e in enumerate(events, start=offset)
                ],
            )
            await conn.execute(
                insert(INBOX),
                [
                    {
                        "event_id": e.event_id,
                        "payload": e.payload(),
                        "attempt_count": 0,
                        "next_at": now,
                        "created_at": now,
                    }
                    for e in events
                ],
            )
        await conn.execute(
            update(COORDINATOR)
            .where(COORDINATOR.c.id == 1)
            .values(pending_count=CAPACITY - 2, accepted_total=CAPACITY - 2)
        )

    async def admit_one(i):
        try:
            return await repo.admit(event(CAPACITY + i))
        except InboxFullError:
            return None

    results = await asyncio.gather(*(admit_one(i) for i in range(8)))
    assert sum(r is not None for r in results) == 2
    assert await count(engine, INBOX) == CAPACITY
    assert (await repo.admit(event())).duplicate
    with pytest.raises(EventConflictError):
        await repo.admit(event(value=73))
    assert (await repo.generate_once()).state == "CAPACITY_PAUSED"
    assert (await repo.snapshot())["skipped_capacity_total"] == 1
    assert (await repo.process_one(0)).state == "COMMITTED"
    assert (await repo.admit(event(CAPACITY + 99))).state == "PENDING"
    assert await count(engine, INBOX) == CAPACITY
    record(
        proof_config,
        "capacity-86400",
        {
            "status": "PASS",
            "actual_pending_rows": CAPACITY,
            "concurrent_remaining_admissions": 2,
            "duplicate_extra_slots": 0,
            "capacity_skip": 1,
        },
    )


@pytest.fixture
def compiled(tmp_path):
    """Compile both production source trees from one actual capture before any worker starts."""
    builder = builder_module()
    snapshot = tmp_path / "captured"
    captured = builder.capture_source_snapshot(snapshot)
    variants = {"worker": snapshot / "demo/vital_worker.py", "capture": captured}
    for revision in ("v1", "v2"):
        destination = tmp_path / revision
        manifest = builder.compile_revision(revision, destination, source_package=snapshot)
        variants[revision] = (destination, manifest)
    assert [key for key, value in variants["v1"][1]["files"].items() if variants["v2"][1]["files"][key] != value] == [
        "revision/write.py"
    ]
    return variants


async def worker(
    config, schema, compiled, revision, *, steps=0, drain_seconds=0, timeout_seconds=30, run_seconds=0, generate=False
):
    """Launch a separate real app-source process; preserve only safe diagnostics and source fingerprints."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(compiled["worker"]),
        "--env-file",
        config["env_file"],
        "--expected-port",
        str(config["port"]),
        "--schema",
        schema,
        "--steps",
        str(steps),
        "--drain-seconds",
        str(drain_seconds),
        "--run-seconds",
        str(run_seconds),
        *(["--generate"] if generate else []),
        env={**os.environ, "PYTHONPATH": str(compiled[revision][0])},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    output_task = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output_task), timeout=timeout_seconds)
    finally:
        await config["reap_phase_group"](process, output_task)
    assert process.returncode == 0, f"worker exit {process.returncode}"
    assert b"synthetic-sensor" not in stdout + stderr and b"P-001" not in stdout + stderr
    result = json.loads(stdout)
    import hashlib

    result["proof_worker_sha256"] = hashlib.sha256(compiled["worker"].read_bytes()).hexdigest()
    assert result["proof_worker_sha256"] == compiled["capture"]["files"]["demo/vital_worker.py"]
    assert result["source"]["verified"] and result["source"]["revision"] == revision
    return result


async def test_actual_compiled_fault_then_new_normal_process_drains_exact_payload_cohort(
    native, proof_config, compiled
):
    engine, repo, schema = native
    # Pre-existing patient query data is separate from the new event/measurement linkage.
    legacy_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            insert(SensorReadingRow.__table__).values(
                id=legacy_id,
                patient_id="legacy-subject",
                reading_type="heart_rate",
                value=65,
                unit="bpm",
                timestamp=datetime(2010, 1, 1, tzinfo=UTC),
                is_abnormal=False,
                created_at=datetime(2010, 1, 1, tzinfo=UTC),
            )
        )
    # Pending before the fault checkpoint must be included, not just later accepted events.
    await repo.admit(event(99))
    start = await repo.checkpoint()
    for number in range(8):
        await repo.admit(event(number))
    end = await repo.checkpoint()
    assert start["lower_exclusive"] == 0 and end["upper_inclusive"] == 9
    fault = await worker(proof_config, schema, compiled, "v2", steps=9)
    errors = [r for r in fault["events"] if r.get("event") == "db_write_error"]
    assert len(errors) == 9 and {r["sqlstate"] for r in errors} == {"42703"}
    assert fault["snapshot"]["pending_count"] == 9 and fault["snapshot"]["committed_total"] == 0
    assert await count(engine, SensorReadingRow.__table__) == 1
    before = await repo.cohort(start["epoch"], start["lower_exclusive"], end["upper_inclusive"])
    assert not before["complete"] and before["pending_events"] == 9
    restored = await worker(proof_config, schema, compiled, "v1", drain_seconds=10)
    assert restored["proof_worker_sha256"] == fault["proof_worker_sha256"]
    assert restored["snapshot"]["pending_count"] == 0 and restored["snapshot"]["committed_total"] == 9
    after = await repo.cohort(start["epoch"], start["lower_exclusive"], end["upper_inclusive"])
    assert after["complete"] and after["matched_measurements"] == 9 and after["payload_mismatches"] == 0
    assert after["observed_42703_events"] == 9 and after["latest_42703_at"]
    assert await count(engine, SensorReadingRow.__table__) == 10
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                select(SensorReadingRow.__table__.c.patient_id).where(SensorReadingRow.__table__.c.id == legacy_id)
            )
        ).scalar_one() == "legacy-subject"
    record(proof_config, "fault-worker", fault)
    record(proof_config, "restored-worker", restored)
    record(
        proof_config,
        "fault-backlog-proof",
        {
            "status": "PASS",
            "actual_sqlstate": "42703",
            "before": before,
            "after": after,
            "old_rows_preserved": True,
            "normal_source": compiled["v1"][1],
            "fault_source": compiled["v2"][1],
        },
    )


async def test_pg_savepoint_failure_keeps_inbox_locked_until_retry_schedule_commit(
    native, proof_config, compiled, monkeypatch, caplog
):
    import importlib.util

    from test_service.adapters.secondary.vital_repository import postgresql

    engine, repo, _ = native
    await repo.admit(event())
    async with engine.begin() as conn:
        await conn.execute(update(INBOX).values(attempt_count=4))
    spec = importlib.util.spec_from_file_location(
        "actual_fault_write", compiled["v2"][0] / "test_service/revision/write.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(postgresql, "write_statement", module.write_statement)
    async with engine.begin() as blocker:
        await blocker.execute(select(COORDINATOR).where(COORDINATOR.c.id == 1).with_for_update())
        failed = asyncio.create_task(repo.process_one(0))
        try:
            blocked = False
            for _ in range(40):
                async with engine.connect() as observer:
                    blocked = (
                        await observer.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name='vital-native-test' AND wait_event_type='Lock' "
                                "AND query LIKE 'UPDATE vital_coordinator%'"
                            )
                        )
                    ).scalar_one() > 0
                if blocked:
                    break
                await asyncio.sleep(0.02)
            assert blocked
            competing = await repo.process_one(1)
            assert competing.state == "NO_WORK"
            assert not failed.done()
        finally:
            pass
    outcome = await failed
    assert outcome.state == "RETRY" and 8 <= outcome.retry_delay_seconds <= 16
    assert (await repo.process_one(1)).state == "NO_WORK"
    assert any(getattr(r, "sqlstate", None) == "42703" for r in caplog.records)
    async with engine.connect() as conn:
        row = (await conn.execute(select(INBOX))).mappings().one()
        assert row["attempt_count"] == 5 and row["next_at"] > datetime.now(UTC)
    assert await count(engine, SensorReadingRow.__table__) == 0
    record(
        proof_config,
        "savepoint-lock-retry",
        {
            "status": "PASS",
            "actual_sqlstate": "42703",
            "competing_worker": "NO_WORK",
            "schedule_committed_before_unlock": True,
        },
    )


async def test_owned_postgresql_restart_preserves_inbox_epoch_and_does_not_replay_slots(native, proof_config):
    engine, repo, _ = native
    await repo.generate_once()
    before = await repo.snapshot()
    label = subprocess.run(
        ["docker", "inspect", "--format", '{{index .Config.Labels "rca.vital.proof"}}', proof_config["container"]],
        capture_output=True,
        text=True,
        check=True,
    )
    assert label.stdout.strip() == proof_config["owner"]
    restarted = await asyncio.create_subprocess_exec(
        "docker",
        "restart",
        "--time",
        "5",
        proof_config["container"],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(restarted.communicate(), timeout=30)
    assert restarted.returncode == 0
    for _ in range(80):
        check = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            proof_config["container"],
            "pg_isready",
            "-U",
            "proof_owner",
            "-d",
            "rca_demo",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await check.communicate()
        if check.returncode == 0:
            break
        await asyncio.sleep(0.25)
    else:
        pytest.fail("owned PostgreSQL did not recover")
    await engine.dispose()
    preserved = await repo.snapshot()
    assert preserved["epoch"] == before["epoch"] and preserved["pending_count"] == before["pending_count"]
    assert preserved["accepted_total"] == before["accepted_total"]
    resumed = await repo.generate_once()
    after = await repo.snapshot()
    if resumed.state == "NO_SLOT":
        assert after["generated_total"] == before["generated_total"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            async with engine.connect() as conn:
                current_slot = int(
                    (await conn.execute(select(func.extract("epoch", func.clock_timestamp())))).scalar_one()
                )
            if current_slot > before["last_slot"]:
                assert (await repo.generate_once()).state == "PENDING"
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("database clock did not reach a new generation slot")
        after = await repo.snapshot()
    assert after["generated_total"] == before["generated_total"] + 1
    while (await repo.snapshot())["pending_count"]:
        assert (await repo.process_one(0)).state == "COMMITTED"
    cohort = await repo.cohort(before["epoch"], 0, after["accepted_total"])
    assert cohort["complete"]
    record(
        proof_config,
        "postgresql-restart",
        {
            "status": "PASS",
            "epoch_preserved": True,
            "pending_preserved": True,
            "post_restart_generated": 1,
            "cohort": cohort,
        },
    )


@pytest.mark.parametrize(
    "corruption",
    ["completed_with_inbox", "pending_without_inbox", "measurement_changed", "measurement_removed", "wrong_epoch"],
)
async def test_native_cohort_rejects_actual_inbox_or_measurement_inconsistency(native, proof_config, corruption):
    from sqlalchemy import delete

    engine, repo, _ = native
    current = event()
    await repo.admit(current)
    checkpoint = await repo.checkpoint()
    if corruption != "pending_without_inbox":
        await repo.process_one(0)
    async with engine.begin() as conn:
        if corruption == "completed_with_inbox":
            now = (await conn.execute(select(func.clock_timestamp()))).scalar_one()
            await conn.execute(
                insert(INBOX).values(
                    event_id=current.event_id, payload=current.payload(), attempt_count=0, next_at=now, created_at=now
                )
            )
        elif corruption == "pending_without_inbox":
            await conn.execute(delete(INBOX))
        elif corruption == "measurement_changed":
            await conn.execute(update(SensorReadingRow.__table__).values(value=75))
        elif corruption == "measurement_removed":
            await conn.execute(delete(SensorReadingRow.__table__))
    status = await repo.cohort("wrong" if corruption == "wrong_epoch" else checkpoint["epoch"], 0, 1)
    assert status["complete"] is False
    if corruption == "completed_with_inbox":
        assert status["pending_events"] == 1
    if corruption == "pending_without_inbox":
        assert status["pending_events"] == 0 and status["lost_pending_inputs"] == 1
    if corruption == "measurement_changed":
        assert status["payload_mismatches"] == 1
    if corruption == "measurement_removed":
        assert status["missing_identities"] == 1
    record(proof_config, "cohort-negative-" + corruption, {"status": "PASS", "verification": status})


async def test_native_worker_timeout_terminates_owned_process_and_releases_database_work(
    native, proof_config, compiled
):
    engine, repo, _ = native
    await repo.admit(event())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION pause_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM pg_sleep(60); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER pause_insert BEFORE INSERT ON sensor_readings "
                "FOR EACH ROW EXECUTE FUNCTION pause_insert()"
            )
        )
    with pytest.raises(TimeoutError):
        await worker(proof_config, native[2], compiled, "v1", steps=1, timeout_seconds=2)
    for _ in range(80):
        async with engine.connect() as conn:
            active = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE application_name='vital-proof-worker'")
                )
            ).scalar_one()
        if active == 0:
            break
        await asyncio.sleep(0.05)
    assert active == 0
    assert await count(engine, SensorReadingRow.__table__) == 0 and await count(engine, INBOX) == 1
    record(
        proof_config,
        "worker-timeout-cleanup",
        {"status": "PASS", "owned_worker_backends": 0, "pending_retained": 1, "measurement_rows": 0},
    )


async def test_insufficient_pool_is_rejected_without_an_unobserved_extra_engine(native):
    from dataclasses import replace
    from types import SimpleNamespace

    from test_service.config import get_settings
    from test_service.di.app_container import AppContainer

    engine, _, _ = native
    container = AppContainer()
    container._settings = replace(get_settings(), db_pool_size=1, db_max_overflow=0)
    container._database = SimpleNamespace(engine=engine)
    with pytest.raises(ValueError, match="pool capacity >= 3"):
        _ = container.vital_service
    assert container._vital_service is None and container._database.engine is engine


async def test_composed_real_service_generation_fault_restart_and_fixed_cohort(native, proof_config, compiled):
    engine, repo, schema = native
    normal = await worker(proof_config, schema, compiled, "v1", run_seconds=3.2, generate=True)
    assert normal["snapshot"]["generated_total"] >= 3 and normal["snapshot"]["committed_total"] >= 2
    assert any(
        r.get("event") == "input_contract_observed" and r.get("event_schema_version") == 2 for r in normal["events"]
    )
    assert any(r.get("event") == "write_completed" for r in normal["events"])
    start = await repo.checkpoint()
    fault = await worker(proof_config, schema, compiled, "v2", run_seconds=4.2, generate=True)
    end = await repo.checkpoint()
    assert fault["snapshot"]["generated_total"] > normal["snapshot"]["generated_total"]
    assert any(r.get("sqlstate") == "42703" for r in fault["events"])
    assert any(r.get("event") == "vital_retry_scheduled" for r in fault["events"])
    assert end["upper_inclusive"] > start["upper_inclusive"]
    before = await repo.cohort(start["epoch"], start["lower_exclusive"], end["upper_inclusive"])
    assert before["expected_events"] > 0 and before["pending_events"] > 0 and not before["complete"]
    async with engine.connect() as conn:
        identities = dict(
            (
                await conn.execute(
                    select(IDENTITY.c.admission_sequence, IDENTITY.c.payload_sha256).where(
                        IDENTITY.c.admission_sequence > start["lower_exclusive"],
                        IDENTITY.c.admission_sequence <= end["upper_inclusive"],
                    )
                )
            ).all()
        )
    accepted_before_gap = (await repo.snapshot())["accepted_total"]
    await asyncio.sleep(2.1)
    assert (await repo.snapshot())["accepted_total"] == accepted_before_gap
    restored = await worker(proof_config, schema, compiled, "v1", run_seconds=6.2, generate=True)
    assert restored["snapshot"]["generated_total"] > fault["snapshot"]["generated_total"]
    after = await repo.cohort(start["epoch"], start["lower_exclusive"], end["upper_inclusive"])
    assert after["complete"] and after["expected_events"] > 0 and after["pending_events"] == 0
    assert after["matched_measurements"] == len(identities) and after["payload_mismatches"] == 0
    assert after["observed_42703_events"] > 0
    assert datetime.fromisoformat(after["latest_42703_at"]) >= datetime.fromisoformat(
        fault["checkpoint_before"]["observed_at"]
    )
    async with engine.connect() as conn:
        current = dict(
            (
                await conn.execute(
                    select(IDENTITY.c.admission_sequence, IDENTITY.c.payload_sha256).where(
                        IDENTITY.c.admission_sequence > start["lower_exclusive"],
                        IDENTITY.c.admission_sequence <= end["upper_inclusive"],
                    )
                )
            ).all()
        )
        max_per_second = (
            await conn.execute(
                text(
                    "SELECT max(n) FROM (SELECT count(*) AS n FROM vital_event_identity "
                    "GROUP BY date_trunc('second',created_at)) AS counts"
                )
            )
        ).scalar_one()
    assert current == identities and max_per_second == 1
    assert normal["proof_worker_sha256"] == fault["proof_worker_sha256"] == restored["proof_worker_sha256"]
    record(proof_config, "composed-normal", normal)
    record(proof_config, "composed-fault", fault)
    record(proof_config, "composed-restored", restored)
    record(
        proof_config,
        "composed-service-proof",
        {
            "status": "PASS",
            "before": before,
            "after": after,
            "actual_vital_service_generate_true": True,
            "max_generated_per_db_second": 1,
            "intentional_gap_new_events": 0,
            "fault_cohort_payload_sha256": identities,
            "source_base_identical": normal["source"]["base_fingerprint"]
            == fault["source"]["base_fingerprint"]
            == restored["source"]["base_fingerprint"],
        },
    )


async def test_native_bootstrap_and_duplicate_fail_closed_on_missing_durable_state(native):
    from sqlalchemy import delete

    engine, repo, _ = native
    current = event()
    await repo.admit(current)
    async with engine.begin() as conn:
        await conn.execute(delete(INBOX))
    with pytest.raises(RuntimeError, match="pending input integrity"):
        await repo.admit(current)
    async with engine.begin() as conn:
        await conn.execute(delete(COORDINATOR))
    with pytest.raises(RuntimeError, match="coordinator missing"):
        await initialize_vital_schema(engine)
    assert await count(engine, IDENTITY) == 1 and await count(engine, COORDINATOR) == 0


async def test_native_distinct_events_have_only_two_global_workers_and_producer_headroom(native, proof_config):
    engine, repo, _ = native
    for number in range(4):
        await repo.admit(event(number))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION pause_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM pg_advisory_xact_lock(hashtext(current_schema()),918778); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER pause_insert BEFORE INSERT ON sensor_readings "
                "FOR EACH ROW EXECUTE FUNCTION pause_insert()"
            )
        )
    tasks = []
    try:
        async with engine.begin() as blocker:
            await blocker.execute(text("SELECT pg_advisory_xact_lock(hashtext(current_schema()),918778)"))
            other = PostgreSQLVitalRepository(engine)
            tasks = [asyncio.create_task((repo if i % 2 else other).process_one(i % 2)) for i in range(4)]
            for _ in range(80):
                async with engine.connect() as observer:
                    waiting = (
                        await observer.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name='vital-native-test' AND wait_event_type='Lock' "
                                "AND query LIKE 'INSERT INTO sensor_readings%'"
                            )
                        )
                    ).scalar_one()
                if waiting == 2:
                    break
                await asyncio.sleep(0.02)
            assert waiting == 2
            assert (await asyncio.wait_for(repo.generate_once(), timeout=1)).state == "PENDING"
    finally:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(getattr(r, "state", None) == "COMMITTED" for r in results) == 2
    assert sum(getattr(r, "state", None) == "BUSY" for r in results) == 2
    assert await count(engine, SensorReadingRow.__table__) == 2
    assert await count(engine, INBOX) == 3
    record(
        proof_config,
        "global-worker-bound",
        {
            "status": "PASS",
            "actual_parallel_insert_waiters": 2,
            "other_callers_busy": 2,
            "producer_progress": True,
            "pool_size": engine.pool.size(),
        },
    )


async def test_native_admission_commit_ack_lost_does_not_duplicate_identity_or_capacity(native, proof_config):
    engine, repo, _ = native
    current = event()

    class LostAdmissionAck:
        @asynccontextmanager
        async def begin(self):
            """Persist the real admission transaction, then lose only its reply."""
            async with engine.begin() as conn:
                yield conn
            raise ConnectionError("simulated admission acknowledgement loss")

    with pytest.raises(ConnectionError):
        await PostgreSQLVitalRepository(LostAdmissionAck()).admit(current)
    assert await count(engine, IDENTITY) == 1 and await count(engine, INBOX) == 1
    assert await count(engine, SensorReadingRow.__table__) == 0
    duplicate = await repo.admit(current)
    assert duplicate.duplicate and duplicate.state == "PENDING"
    snapshot = await repo.snapshot()
    assert snapshot["accepted_total"] == snapshot["pending_count"] == 1 and snapshot["committed_total"] == 0
    assert (await repo.process_one(0)).state == "COMMITTED"
    completed = await repo.admit(current)
    assert completed.duplicate and completed.state == "COMPLETED"
    assert await count(engine, IDENTITY) == 1 and await count(engine, INBOX) == 0
    assert await count(engine, SensorReadingRow.__table__) == 1
    snapshot = await repo.snapshot()
    assert snapshot["accepted_total"] == snapshot["committed_total"] == 1
    record(
        proof_config,
        "admission-ack-loss",
        {
            "status": "PASS",
            "admission_reply": "lost",
            "retry_state": "PENDING",
            "accepted_total": 1,
            "unique_identity": 1,
            "unique_measurement": 1,
            "pending_after_completion": 0,
        },
    )


async def test_native_upgrade_legacy_rows_then_reinitialize_preserves_epoch_and_pending(proof_config):
    import hashlib

    schema = "vital_" + uuid.uuid4().hex
    admin = create_async_engine(proof_config["dsn"], poolclass=NullPool, hide_parameters=True)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        proof_config["dsn"],
        pool_size=5,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda connection: SensorReadingRow.__table__.create(connection))
            await conn.execute(
                insert(SensorReadingRow.__table__).values(
                    id=str(uuid.uuid4()),
                    patient_id="legacy-subject",
                    reading_type="heart_rate",
                    value=70,
                    unit="bpm",
                    timestamp=datetime(2010, 1, 1, tzinfo=UTC),
                    is_abnormal=False,
                    created_at=datetime(2010, 1, 1, tzinfo=UTC),
                )
            )

        async def legacy_signature():
            """Compare original values privately and expose only a digest in proof output."""
            async with engine.connect() as conn:
                rows = [dict(row) for row in (await conn.execute(select(SensorReadingRow.__table__))).mappings()]
                columns = (
                    await conn.execute(
                        text(
                            "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
                            "WHERE table_schema=:schema AND table_name='sensor_readings' ORDER BY ordinal_position"
                        ),
                        {"schema": schema},
                    )
                ).all()
                tables = (
                    (
                        await conn.execute(
                            text("SELECT tablename FROM pg_tables WHERE schemaname=:schema ORDER BY tablename"),
                            {"schema": schema},
                        )
                    )
                    .scalars()
                    .all()
                )
            return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest(), columns, tables

        before = await legacy_signature()
        assert before[2] == ["sensor_readings"]
        await initialize_vital_schema(engine)
        repo = PostgreSQLVitalRepository(engine)
        await repo.admit(event())
        state = await repo.snapshot()
        await initialize_vital_schema(engine)
        assert await repo.snapshot() == state
        after = await legacy_signature()
        assert before[:2] == after[:2]
        assert set(after[2]) == {"sensor_readings", "vital_event_identity", "vital_inbox", "vital_coordinator"}
        assert await count(engine, INBOX) == 1 and await count(engine, IDENTITY) == 1
        record(
            proof_config,
            "legacy-upgrade-reinitialize",
            {
                "status": "PASS",
                "legacy_row_sha256": before[0],
                "old_columns_unchanged": True,
                "epoch_unchanged": True,
                "pending_preserved": 1,
                "counters_unchanged": True,
                "before_tables": before[2],
                "after_tables": after[2],
            },
        )
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
