"""Pinned native producer data; AWS provenance wrappers are fixtures, never live baseline proof."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from headless_codex.services.recovery_evidence import (
    _VITAL_INPUT_PATHS,
    _contract,
    _hash,
    prepare_recovery_evidence,
    verify_input_compatibility,
)
from headless_codex.services.recovery_observation import _safe_message


@pytest.fixture
def native():
    """Keep raw native capture/source hashes immutable and explicit about its post-restore normal sample."""
    path = Path(__file__).resolve().parents[2] / "agent/tests/fixtures/vital_native_contract.json"
    return json.loads(path.read_text())


@pytest.fixture
def joined(native):
    """Supply fake task/log envelopes around unchanged actual native input and outcome messages."""
    groups = []
    for phase in ("normal", "current"):
        sample = native[phase]
        stamp = datetime.fromisoformat(sample["input"]["observed_at"])

        def row(name, message, observed, phase=phase):
            """Label the test-only AWS wrapper while retaining actual emitted message contents."""
            return {
                "event_id": name,
                "timestamp": int(observed.timestamp() * 1000),
                "log_group": "/local-native-fixture",
                "log_stream": f"local/app/{phase}",
                "task_definition_arn": phase,
                "image_digest": phase,
                "message": message,
            }

        groups.append(
            {
                "observations": [
                    row(
                        "source-" + phase,
                        {"event": "source_manifest", **sample["source"]},
                        stamp - timedelta(milliseconds=1),
                    ),
                    row("input-" + phase, sample["input"], stamp),
                    row(
                        "outcome-" + phase, sample["outcome"], datetime.fromisoformat(sample["outcome"]["observed_at"])
                    ),
                ]
            }
        )
    baseline, current = groups
    stamp = datetime.fromisoformat(native["normal"]["outcome"]["observed_at"])
    baseline.update(
        scope={"log_group": "/local-native-fixture", "container_name": "app"},
        normal={"task_definition_arn": "normal", "image_digest": "normal"},
        metric_observations={
            "start": (stamp - timedelta(seconds=1)).isoformat(),
            "end": (stamp + timedelta(seconds=1)).isoformat(),
        },
    )
    current.update(
        task_definition_arn="current",
        image_digest="current",
        log_stream_prefix="local",
        tasks=[{"task_arn": "task/current"}],
        log_window={
            "coverage": dict.fromkeys(("source_manifest", "input_contract_observed", "db_write_error"), "complete")
        },
    )
    return baseline, current


def test_pinned_native_descriptor_and_actual_v2_commit(joined, native):
    """Real PostgreSQL-emitted commit/error samples satisfy the consumer without new declarations."""
    before = deepcopy(joined)
    result = verify_input_compatibility(*joined)
    assert result["status"] == "VERIFIED" and result["input_format"] == "vital-event-v1-v2"
    assert {w["event_schema_version"] for w in result["witnesses"]} == {2}
    assert native["current"]["outcome"]["sqlstate"] == "42703"
    assert native["normal"]["outcome"]["count"] == 1
    assert joined == before
    for phase in ("normal", "current"):
        message = native[phase]["input"]
        descriptor = message["input_contract"]
        assert set(descriptor["source_files"]) == _VITAL_INPUT_PATHS
        assert (
            descriptor["source_files"]["services/input_contract.py"]
            == "254c50f96141bba8f102b04debfa7566fc772f0b7b0b9a1f78c340b339f2ef81"
        )
        assert _hash(descriptor) == message["input_contract_sha256"]
        assert all(native[phase]["source"]["files"][p] == v for p, v in descriptor["source_files"].items())
        safe = _safe_message(message)
        assert safe["event_schema_version"] == 2
        assert safe["input_contract"] == descriptor


@pytest.mark.parametrize(
    "change",
    [
        "normal_v1",
        "missing_version",
        "bool_version",
        "unknown_version",
        "admission",
        "duplicate",
        "missing_path",
        "model_tamper",
        "stage",
        "bool_versions",
        "wrong_mapping",
        "foreign_image",
        "incomplete",
        "legacy_mixed",
    ],
)
def test_vital_counterexamples_never_infer_compatibility(joined, change):
    """Schema declarations, ACKs, old normal inputs and wrong source cannot stand in for v2 commits."""
    baseline, current = deepcopy(joined)
    receipt = current["observations"][1]["message"]
    descriptor = receipt["input_contract"]
    if change == "normal_v1":
        baseline["observations"][1]["message"]["event_schema_version"] = 1
    elif change == "missing_version":
        receipt.pop("event_schema_version")
    elif change == "bool_version":
        receipt["event_schema_version"] = True
    elif change == "unknown_version":
        receipt["event_schema_version"] = 3
    elif change == "admission":
        baseline["observations"][2]["message"]["event"] = "inbox_accepted"
    elif change == "duplicate":
        baseline["observations"][2]["message"]["count"] = 0
    elif change == "missing_path":
        descriptor["source_files"].pop("adapters/secondary/sensor_repository/models.py")
    elif change == "model_tamper":
        descriptor["source_files"]["adapters/secondary/vital_repository/models.py"] = "0" * 64
    elif change == "stage":
        descriptor["delivery_stage"] = "inbox_admission"
    elif change == "bool_versions":
        descriptor["event_versions"] = [True, 2]
    elif change == "wrong_mapping":
        descriptor["timestamp_mapping"] = "sampled_at-to-sampled_at"
    elif change == "foreign_image":
        current["observations"][1]["image_digest"] = "foreign"
    elif change == "incomplete":
        current["log_window"]["coverage"]["input_contract_observed"] = "partial"
    else:
        descriptor["format"] = "sensor-service-batch-v1"
    receipt["input_contract_sha256"] = _hash(descriptor)
    with pytest.raises(ValueError):
        verify_input_compatibility(baseline, current)


def test_native_event_fields_are_required_at_the_descriptor_boundary(native):
    """Keep producer stage/version semantics separate from arbitrary metadata and preserve raw records."""
    sample = native["normal"]
    assert (
        _contract({"message": sample["input"]}, {"message": sample["source"]})[0]
        == sample["input"]["input_contract_sha256"]
    )
    assert (
        sample["source"]["fingerprint"]
        == hashlib.sha256(json.dumps(sample["source"]["files"], sort_keys=True).encode()).hexdigest()
    )


pytest_plugins = ["recovery_fixture"]


@pytest.mark.parametrize("normal_version", [1, 2])
def test_native_reader_receipt_preserves_version_and_requires_v2_commit(setup_observations, native, normal_version):
    """Exercise the real reader/receipt/approval boundary with local AWS envelopes and pinned input descriptors."""
    from types import SimpleNamespace

    reader, alarm, baseline, publish, _, logs, _ = setup_observations
    baseline["observations"][0]["message"].update(native["normal"]["source"])
    write = baseline["observations"][2]
    write["message"].update(count=1, request_id=native["normal"]["input"]["request_id"], operation="ingest")
    receipt = deepcopy(write)
    receipt["event_id"] = "vital-normal-input"
    receipt["message"] = {
        **native["normal"]["input"],
        "observed_at": write["message"]["observed_at"],
        "event_schema_version": normal_version,
    }
    baseline["observations"].append(receipt)
    events = logs.filter_log_events.return_value["events"]
    for event in events:
        message = json.loads(event["message"])
        if message["event"] == "source_manifest":
            message.update(native["current"]["source"])
        elif message["event"] == "db_write_error":
            message["request_id"] = native["current"]["input"]["request_id"]
        event["message"] = json.dumps(message)
    current_receipt = deepcopy(events[0])
    current_receipt["eventId"] = "vital-current-input"
    current_receipt["message"] = json.dumps(
        {**native["current"]["input"], "observed_at": alarm.state_change_time.isoformat()}
    )
    events.append(current_receipt)
    publish()
    observations = reader.observe(alarm, timeout_seconds=90)
    before = observations.model_dump(mode="json")
    result = prepare_recovery_evidence(SimpleNamespace(raw_alarm=alarm, incident_observations=observations))
    assert result["verification"]["status"] == ("VERIFIED" if normal_version == 2 else "UNAVAILABLE")
    assert observations.model_dump(mode="json") == before
    if normal_version == 2:
        assert result["context"]
        assert {w["event_schema_version"] for w in result["verification"]["witnesses"]} == {2}
