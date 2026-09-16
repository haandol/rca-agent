"""Immutable source, commit ordering and native PostgreSQL regression contracts."""

import importlib.util
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_service.adapters.secondary.sensor_repository.sqlalchemy_sensor_repository import (
    SqlAlchemySensorReadingRepository,
)
from test_service.ports.dto.sensor import SensorReadingEntity
from test_service.services.write_diagnostics import log_write_error

PACKAGE = Path(__file__).resolve().parents[1]


def builder_module():
    """Load the build tool without installing proof helpers into the production wheel."""
    spec = importlib.util.spec_from_file_location("write_builder", PACKAGE / "demo/build_revision.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compiled_revisions_differ_only_in_one_constant_and_ignore_runtime_flags(tmp_path):
    """One captured source supplies both artifacts and rejects runtime relabeling or tampering."""
    builder = builder_module()
    snapshot = tmp_path / "snapshot"
    builder.capture_source_snapshot(snapshot)
    manifests = {v: builder.compile_revision(v, tmp_path / v, source_package=snapshot) for v in ("v1", "v2")}
    first, second = (manifests[v] for v in ("v1", "v2"))
    assert first["base_fingerprint"] == second["base_fingerprint"]
    assert [p for p in first["files"] if first["files"][p] != second["files"][p]] == ["revision/write.py"]
    write = "test_service/revision/write.py"
    assert (tmp_path / "v1" / write).read_text().replace(
        'TIMESTAMP_COLUMN = "timestamp"', 'TIMESTAMP_COLUMN = "sampled_at"'
    ) == (tmp_path / "v2" / write).read_text()
    for version in ("v1", "v2"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from test_service.revision.manifest import source_manifest; "
                "print(json.dumps(source_manifest()))",
            ],
            env={
                **os.environ,
                "PYTHONPATH": str(tmp_path / version),
                "SOURCE_REVISION": "v2",
                "DEPLOYED_REVISION": "unrelated",
            },
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        observed = json.loads(result.stdout)
        assert observed["revision"] == version and observed["verified"]
        with pytest.raises(ValueError, match="already compiled"):
            builder.compile_revision(version, tmp_path / version, in_place=True)
    path = tmp_path / "v1" / write
    path.write_text(path.read_text() + "\n# mutation\n")
    result = subprocess.run(
        [sys.executable, "-c", "from test_service.revision.manifest import source_manifest; source_manifest()"],
        env={**os.environ, "PYTHONPATH": str(tmp_path / "v1")},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode and "differs from the build manifest" in result.stderr
    for retired in ("r1", "r2", "r3", "unknown"):
        with pytest.raises(ValueError, match="Unknown source revision"):
            builder.compile_revision(retired, tmp_path / retired)


@pytest.mark.parametrize("fail_commit", [False, True])
async def test_completion_is_emitted_only_after_successful_commit(caplog, fail_commit):
    """Successful execution alone is insufficient: commit errors must never emit completion."""
    committed = False
    session = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def scope():
        nonlocal committed
        yield session
        assert not [r for r in caplog.records if getattr(r, "event", None) == "write_completed"]
        if fail_commit:
            raise RuntimeError("commit failed")
        committed = True

    row = SensorReadingEntity("id", "private", "heart_rate", 72, "bpm", datetime.now(UTC))
    repository = SqlAlchemySensorReadingRepository(SimpleNamespace(session_context=scope))
    with caplog.at_level(logging.INFO):
        if fail_commit:
            with pytest.raises(RuntimeError):
                await repository.save_batch([row])
        else:
            assert await repository.save_batch([row]) == [row]
    events = [r for r in caplog.records if getattr(r, "event", None) == "write_completed"]
    assert committed == (not fail_commit)
    assert len(events) == int(not fail_commit)
    if events:
        assert events[0].count == 1 and events[0].completion_semantics == "committed_rows"
        assert events[0].request_id
    assert "private" not in json.dumps([r.__dict__ for r in caplog.records], default=str)


def test_missing_driver_attributes_are_not_inferred_from_message(caplog):
    """Messages resembling PostgreSQL diagnostics cannot create invented observed fields."""
    with caplog.at_level(logging.ERROR):
        log_write_error(RuntimeError("SQLSTATE 42703 table secret_table column secret_column password=CANARY"))
    event = caplog.records[-1]
    assert event.sqlstate is None and event.schema_name is None and event.driver_column_name is None
    assert "CANARY" not in json.dumps(event.__dict__, default=str)


@pytest.mark.skipif(not os.environ.get("HEALTHCARE_PROOF_ENV_FILE"), reason="Requires explicitly owned PostgreSQL")
def test_native_postgresql_same_schema_round_trip(tmp_path):
    """Run the complete native HTTP and transaction proof, never substituting a synthetic DB error."""
    result = subprocess.run(
        [
            sys.executable,
            str(PACKAGE / "demo/local_runner.py"),
            "--env-file",
            os.environ["HEALTHCARE_PROOF_ENV_FILE"],
            "--output",
            str(tmp_path / "proof"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout
    evidence = json.loads((tmp_path / "proof/evidence.json").read_text())
    assert all(evidence["checks"].values())
    assert evidence["schema_removed"] and not evidence["cleanup_errors"]


@pytest.mark.parametrize(
    "body,healthy",
    [
        ({"status": "ok", "db_connected": True}, True),
        ({"status": "degraded", "db_connected": False}, False),
        ({"status": "ok", "db_connected": False}, False),
    ],
)
def test_container_healthcheck_reads_body_not_just_http_success(monkeypatch, body, healthy):
    """Execute the shipped Docker probe against HTTP-200 bodies, including degraded results."""
    import io
    import urllib.request

    command_line = next(
        line for line in (PACKAGE / "Dockerfile").read_text().splitlines() if 'CMD ["python", "-c"' in line
    )
    command = json.loads(command_line.strip().removeprefix("CMD "))[-1]
    monkeypatch.setattr(urllib.request, "urlopen", lambda _: io.StringIO(json.dumps(body)))
    if healthy:
        exec(command, {})
    else:
        with pytest.raises(AssertionError):
            exec(command, {})


def test_snapshot_remains_frozen_after_workspace_edit(tmp_path, monkeypatch):
    """Concurrent source edits cannot silently make the second revision use a different base."""
    import shutil

    builder = builder_module()
    workspace = tmp_path / "workspace"
    shutil.copytree(PACKAGE / "src", workspace / "src")
    (workspace / "demo").mkdir()
    for name in ("local_worker.py", "build_revision.py"):
        shutil.copyfile(PACKAGE / "demo" / name, workspace / "demo" / name)
    monkeypatch.setattr(builder, "PACKAGE", workspace)
    snapshot = tmp_path / "snapshot"
    builder.capture_source_snapshot(snapshot)
    (workspace / "src/test_service/revision/write.py").write_text("# concurrent workspace change\n")
    manifests = [builder.compile_revision(v, tmp_path / v, source_package=snapshot) for v in ("v1", "v2")]
    assert manifests[0]["base_fingerprint"] == manifests[1]["base_fingerprint"]
    assert "concurrent workspace change" not in (tmp_path / "v2/test_service/revision/write.py").read_text()


async def test_retired_control_routes_are_absent(client):
    """No HTTP entry point can select an old mechanism after the single-scenario replacement."""
    for route in ("/fault/db-leak", "/fault/high-cpu", "/fault/slow-query", "/fault/reset"):
        assert (await client.post(route, json={})).status_code == 404
