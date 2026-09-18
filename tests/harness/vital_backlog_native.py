"""Own a disposable local PostgreSQL container; execute root proof against actual compiled fault/normal code."""

import asyncio
import hashlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(tempfile.mkdtemp(prefix="rca-vital-root-proof-"))
sys.path.insert(0, str(ROOT / "packages/healthcare-sensor-app/src"))
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_service.adapters.secondary.sensor_repository.models import SensorReadingRow
from test_service.adapters.secondary.vital_repository.postgresql import (
    PostgreSQLVitalRepository,
    initialize_vital_schema,
)
from test_service.ports.dto.vital import VitalEvent

name = "rca-vital-root-" + uuid.uuid4().hex[:8]
password = secrets.token_hex(20)
env = OUT / "postgres.env"
env.write_text(
    "POSTGRES_USER=proof\nPOSTGRES_DB=rca_demo\nPOSTGRES_PASSWORD=" + password + "\n"
)
env.chmod(0o600)
try:
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "rca.proof.owner=" + name,
            "--env-file",
            str(env),
            "-p",
            "127.0.0.1::5432",
            "postgres:17",
        ],
        check=True,
        capture_output=True,
        timeout=90,
    )
    port = int(
        subprocess.check_output(
            ["docker", "port", name, "5432/tcp"], text=True, timeout=90
        )
        .strip()
        .rsplit(":", 1)[1]
    )
    dsn = f"postgresql+asyncpg://proof:{password}@127.0.0.1:{port}/rca_demo"
    path = OUT / "database.env"
    path.write_text("DATABASE_URL=" + dsn + "\n")
    path.chmod(0o600)
    for _ in range(40):
        if (
            subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "proof",
                    "-d",
                    "rca_demo",
                ],
                capture_output=True,
                check=False,
                timeout=90,
            ).returncode
            == 0
        ):
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("owned PostgreSQL not ready")
    spec = importlib.util.spec_from_file_location(
        "root_demo", ROOT / "scripts/run_realistic_demo.py"
    )
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    schema = "vital_" + uuid.uuid4().hex
    snap = {
        "options": {"container": "healthcare"},
        "taskDefinition": {
            "taskDefinition": {
                "containerDefinitions": [
                    {
                        "name": "healthcare",
                        "environment": [
                            {"name": "DB_HOST", "value": "127.0.0.1"},
                            {"name": "DB_PORT", "value": str(port)},
                            {"name": "DB_NAME", "value": "rca_demo"},
                        ],
                    }
                ]
            }
        },
        "normalObservations": [
            {"message": {"event": "db_schema_snapshot", "schema_name": schema}}
        ],
    }

    def proof(operation, **kwargs):
        """Invoke the production root subprocess against this owned database, not a summary fixture."""
        return demo.read_vital_database(snap, path, operation, **kwargs)

    async def run():
        """Keep historical rows while actual compiled fault SQL and normal code exercise the readonly proof."""
        admin = create_async_engine(dsn, hide_parameters=True)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            dsn,
            hide_parameters=True,
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            await initialize_vital_schema(engine)
            repo = PostgreSQLVitalRepository(engine)
            async with engine.begin() as conn:
                await conn.execute(
                    SensorReadingRow.__table__.insert().values(
                        id="existing-row",
                        patient_id="PRIVATE_OLD",
                        reading_type="heart_rate",
                        value=72,
                        unit="bpm",
                        timestamp=datetime.now(timezone.utc),
                        is_abnormal=False,
                        created_at=datetime.now(timezone.utc),
                    )
                )
            opened = proof("checkpoint")["result"]
            assert opened["upper_inclusive"] == 0
            for i in range(3):
                await repo.admit(
                    VitalEvent(
                        "root-event-" + str(i),
                        "PRIVATE_SENSOR",
                        "PRIVATE_PATIENT",
                        2,
                        "heart_rate",
                        70 + i,
                        "bpm",
                        datetime(2020, 1, 1, tzinfo=timezone.utc),
                    )
                )
            spec = importlib.util.spec_from_file_location(
                "builder",
                ROOT / "packages/healthcare-sensor-app/demo/build_revision.py",
            )
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            capture = OUT / "capture"
            builder.capture_source_snapshot(capture)
            installed = OUT / "fault"
            builder.compile_revision("v2", installed, source_package=capture)
            worker = await asyncio.to_thread(
                subprocess.run,
                [
                    str(ROOT / "packages/healthcare-sensor-app/.venv/bin/python"),
                    str(ROOT / "packages/healthcare-sensor-app/demo/vital_worker.py"),
                    "--env-file",
                    str(path),
                    "--expected-port",
                    str(port),
                    "--schema",
                    schema,
                    "--steps",
                    "3",
                ],
                env={**os.environ, "PYTHONPATH": str(installed)},
                capture_output=True,
                text=True,
                check=True,
                timeout=90,
            )
            fault = json.loads(worker.stdout)
            assert (
                len([e for e in fault["events"] if e.get("sqlstate") == "42703"]) == 3
            )
            refs = [a["reading_id"] for a in fault["results"] if a["state"] == "RETRY"]
            assert len(refs) == 3
            closed = proof("checkpoint")["result"]
            bounds = {
                "epoch": opened["epoch"],
                "lower_exclusive": opened["lower_exclusive"],
                "upper_inclusive": closed["upper_inclusive"],
            }
            before = proof("cohort", cohort=bounds)
            assert (
                before["result"]["pending_events"] == 3
                and not before["result"]["complete"]
            )
            await asyncio.sleep(1.1)
            for _ in range(3):
                assert (await repo.process_one(0)).state == "COMMITTED"
            after = proof("cohort", cohort=bounds)
            assert (
                after["result"]["complete"]
                and after["result"]["matched_measurements"] == 3
            )
            await repo.admit(
                VitalEvent(
                    "outside-cohort",
                    "PRIVATE_SENSOR",
                    "PRIVATE_PATIENT",
                    2,
                    "heart_rate",
                    75,
                    "bpm",
                    datetime.now(timezone.utc),
                )
            )
            after_new = proof("cohort", cohort=bounds)
            assert after_new["result"]["complete"]
            async with engine.connect() as conn:
                assert (
                    await conn.execute(
                        select(func.count()).select_from(SensorReadingRow.__table__)
                    )
                ).scalar_one() == 4
            wrong = proof("cohort", cohort={**bounds, "epoch": str(uuid.uuid4())})
            assert not wrong["result"]["complete"]
            normal = OUT / "normal"
            normal_manifest = builder.compile_revision(
                "v1", normal, source_package=capture
            )

            async def database_state():
                """Compare every persisted proof table before/after readers without publishing private rows."""
                digests = {}
                async with engine.connect() as conn:
                    for table in (
                        "sensor_readings",
                        "vital_event_identity",
                        "vital_inbox",
                        "vital_coordinator",
                    ):
                        rows = (
                            (
                                await conn.execute(
                                    text(
                                        f'SELECT to_jsonb(t)::text FROM "{schema}"."{table}" t ORDER BY to_jsonb(t)::text'
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        digests[table] = hashlib.sha256(
                            json.dumps(rows).encode()
                        ).hexdigest()
                return digests

            async def packaged(operation, selectors=()):
                """Execute the actual compiled normal module and bind its safe stdout to the installed bytes."""
                probe_id = uuid.uuid4().hex
                probe = await asyncio.to_thread(
                    subprocess.run,
                    [
                        str(ROOT / "packages/healthcare-sensor-app/.venv/bin/python"),
                        "-m",
                        "test_service.backlog_probe",
                        "--operation",
                        operation,
                        "--probe-id",
                        probe_id,
                        "--run-id",
                        "native-root-proof",
                        "--schema",
                        schema,
                        *selectors,
                    ],
                    env={**os.environ, "PYTHONPATH": str(normal), "DATABASE_URL": dsn},
                    cwd=OUT,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=90,
                    start_new_session=True,
                )
                assert not probe.stderr.strip()
                packet = json.loads(probe.stdout)
                assert packet["probe_id"] == probe_id
                assert packet["operation"] == operation
                assert packet["source_fingerprint"] == normal_manifest["fingerprint"]
                assert (
                    packet["probe_source_sha256"]
                    == normal_manifest["files"]["backlog_probe.py"]
                )
                return packet

            before_readers = await database_state()
            checkpoint_packet = await packaged("checkpoint")
            direct_checkpoint = proof("checkpoint")["result"]
            for key in ("epoch", "lower_exclusive", "upper_inclusive"):
                assert checkpoint_packet["result"][key] == direct_checkpoint[key]
            packet = await packaged(
                "cohort",
                [
                    "--epoch",
                    bounds["epoch"],
                    "--lower-exclusive",
                    str(bounds["lower_exclusive"]),
                    "--upper-inclusive",
                    str(bounds["upper_inclusive"]),
                ],
            )
            assert packet["result"] == after_new["result"]
            assert packet["result"]["complete"]
            assert packet["result"]["observed_42703_events"] == 3
            assert await database_state() == before_readers
            result = {
                "status": "PASS",
                "transport": "actual compiled normal packaged CLI and direct PostgreSQL readers",
                "compiled_fault_sqlstate": "42703",
                "fault_error_count": 3,
                "before": before,
                "after": after,
                "after_new_admission": after_new["result"],
                "wrong_epoch": wrong["result"],
                "old_row_preserved": True,
                "packaged_normal_probe": packet,
                "packaged_checkpoint": checkpoint_packet,
                "all_persisted_tables_unchanged_by_readers": True,
                "container": name,
                "scope": "task-owned loopback DB only; no AWS",
            }
            encoded = json.dumps(result, indent=2)
            assert (
                "PRIVATE_SENSOR" not in encoded
                and "PRIVATE_PATIENT" not in encoded
                and "PRIVATE_OLD" not in encoded
                and password not in encoded
            )
            (OUT / "result.json").write_text(encoded + "\n")
            print("NATIVE_ROOT_PROOF_PASS", OUT / "result.json", flush=True)
        finally:
            await engine.dispose()
            await admin.dispose()

    asyncio.run(run())
finally:
    try:
        subprocess.run(
            ["docker", "rm", "-f", "-v", name],
            check=True,
            capture_output=True,
            timeout=90,
        )
    except (subprocess.SubprocessError, OSError):
        (OUT / "cleanup.json").write_text(
            json.dumps({"container": name, "status": "UNKNOWN"}) + "\n"
        )
        raise RuntimeError("owned local container cleanup UNKNOWN") from None
    inspected = subprocess.run(
        ["docker", "container", "inspect", name, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert inspected.returncode != 0 and "No such" in inspected.stderr
    (OUT / "cleanup.json").write_text(
        json.dumps(
            {
                "container": name,
                "status": "VERIFIED_REMOVED",
                "anonymous_volumes_removed": True,
            }
        )
        + "\n"
    )
    print("OWNED_CONTAINER_REMOVED", name, flush=True)
