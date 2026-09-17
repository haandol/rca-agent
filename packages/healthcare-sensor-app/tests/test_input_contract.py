"""Input observation preserves current behavior and joins actual commit ordering."""

import hashlib
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_service.adapters.secondary.sensor_repository.sqlalchemy_sensor_repository import (
    SqlAlchemySensorReadingRepository,
)
from test_service.services.input_contract import input_contract
from test_service.services.sensor import SensorService
from tests.test_write_revision import builder_module


@pytest.mark.parametrize("fail_commit", [False, True])
async def test_input_receipt_precedes_write_and_shares_real_request(caplog, fail_commit):
    @asynccontextmanager
    async def scope():
        inputs = [r for r in caplog.records if getattr(r, "event", None) == "input_contract_observed"]
        assert len(inputs) == 1
        assert not [r for r in caplog.records if getattr(r, "event", None) == "write_completed"]
        yield SimpleNamespace(execute=AsyncMock())
        if fail_commit:
            raise RuntimeError("commit failed")

    service = SensorService(SqlAlchemySensorReadingRepository(SimpleNamespace(session_context=scope)))
    stamp = datetime(2026, 9, 1, tzinfo=UTC)
    reading = {
        "patient_id": "PRIVATE_PATIENT",
        "reading_type": "heart_rate",
        "value": 72,
        "unit": "bpm",
        "timestamp": stamp,
    }
    with caplog.at_level(logging.INFO):
        if fail_commit:
            with pytest.raises(RuntimeError):
                await service.ingest([reading])
        else:
            saved = await service.ingest([reading])
            assert saved[0].timestamp == stamp
    receipts = [r for r in caplog.records if getattr(r, "event", None) == "input_contract_observed"]
    outcomes = [r for r in caplog.records if getattr(r, "event", None) in {"write_completed", "db_write_error"}]
    assert len(receipts) == len(outcomes) == 1
    assert receipts[0].request_id == outcomes[0].request_id
    assert receipts[0].input_contract["format"] == "sensor-service-batch-v1"
    assert "PRIVATE_PATIENT" not in str([r.__dict__ for r in caplog.records])


async def test_unknown_future_shape_keeps_behavior_but_has_no_witness(caplog):
    repository = SimpleNamespace(save_batch=AsyncMock(side_effect=lambda rows: rows))
    with caplog.at_level(logging.INFO):
        saved = await SensorService(repository).ingest(
            [
                {
                    "patient_id": "private",
                    "reading_type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                    "schema_version": "v2",
                    "sampled_at": "2026-09-01T00:00:00Z",
                }
            ]
        )
    assert len(saved) == 1
    assert not [r for r in caplog.records if getattr(r, "event", None) == "input_contract_observed"]


def test_descriptor_hash_covers_both_entrypoints_and_common_normalization():
    receipt = input_contract()
    descriptor = receipt["input_contract"]
    assert (
        receipt["input_contract_sha256"]
        == hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )
    assert {"services/sensor.py", "services/traffic_generator.py", "adapters/primary/schemas.py"} <= set(
        descriptor["source_files"]
    )
    assert "revision/write.py" not in descriptor["source_files"]


def test_actual_compiled_pair_preserves_input_contract_and_changes_only_sql(tmp_path):
    builder = builder_module()
    snapshot = tmp_path / "snapshot"
    builder.capture_source_snapshot(snapshot)
    receipts = []
    for revision in ("v1", "v2"):
        destination = tmp_path / revision
        manifest = builder.compile_revision(revision, destination, source_package=snapshot)
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from test_service.services.input_contract import input_contract; "
                "print(json.dumps(input_contract()))",
            ],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(destination)},
            capture_output=True,
            text=True,
            check=True,
        )
        receipt = json.loads(process.stdout)
        for path, digest in receipt["input_contract"]["source_files"].items():
            assert manifest["files"][path] == digest
        receipts.append(receipt)
    assert receipts[0] == receipts[1]
