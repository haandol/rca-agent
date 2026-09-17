"""Early eligibility uses actual reader receipts, not serialized verification flags."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from rca_agent.ports.dto.observations import IncidentObservations
from rca_agent.services.deployment_baseline import build_rollback_context
from rca_agent.services.recovery_evidence import _INPUT_PATHS, _hash, prepare_recovery_evidence

pytest_plugins = ["tests.test_incident_observation"]


@pytest.fixture
def compatible(setup_observations):
    reader, alarm, baseline, publish, _, logs, _ = setup_observations
    files = {path: hashlib.sha256(path.encode()).hexdigest() for path in _INPUT_PATHS}
    files["revision/write.py"] = "1" * 64
    descriptor = {
        "format": "sensor-service-batch-v1",
        "timestamp_mapping": "timestamp-or-server-utc",
        "source_files": {path: files[path] for path in _INPUT_PATHS},
        "runtime": {"python": "3.13.7", "pydantic": "2.11.7"},
    }
    contract = {"input_contract": descriptor, "input_contract_sha256": _hash(descriptor)}
    source = baseline["observations"][0]["message"]
    source.update(files=files, fingerprint=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())
    write = baseline["observations"][2]
    write["message"]["request_id"] = "a" * 32
    write["message"]["operation"] = "ingest"
    receipt = deepcopy(write)
    receipt["event_id"] = "input-normal"
    receipt["message"] = {
        **contract,
        "event": "input_contract_observed",
        "observed_at": write["message"]["observed_at"],
        "operation": "ingest",
        "request_id": "a" * 32,
        "count": 2,
    }
    baseline["observations"].append(receipt)
    events = logs.filter_log_events.return_value["events"]
    current_files = {**files, "revision/write.py": "2" * 64}
    for event in events:
        message = json.loads(event["message"])
        if message["event"] == "source_manifest":
            message.update(
                files=current_files,
                fingerprint=hashlib.sha256(json.dumps(current_files, sort_keys=True).encode()).hexdigest(),
            )
        if message["event"] == "db_write_error":
            message["request_id"] = "b" * 32
        event["message"] = json.dumps(message)
    receipt = deepcopy(events[0])
    receipt["eventId"] = "input-current"
    receipt["message"] = json.dumps(
        {
            **contract,
            "event": "input_contract_observed",
            "observed_at": alarm.state_change_time.isoformat(),
            "operation": "ingest",
            "request_id": "b" * 32,
            "count": 2,
        }
    )
    events.append(receipt)
    publish()
    return setup_observations


def _prepare(fixture):
    reader, alarm, *_ = fixture
    observed = reader.observe(alarm, timeout_seconds=90)
    scoped = SimpleNamespace(raw_alarm=alarm, incident_observations=observed)
    return scoped, prepare_recovery_evidence(scoped)


def test_real_reader_joins_normal_commit_current_failure_and_installed_input(compatible):
    scoped, result = _prepare(compatible)
    assert result["context"] == build_rollback_context(scoped)
    assert result["verification"]["status"] == "VERIFIED"
    assert result["verification"]["input_format"] == "sensor-service-batch-v1"
    assert {w["phase"] for w in result["verification"]["witnesses"]} == {"normal", "current"}
    assert result["frozen_observations"]["current"]["observations"]
    assert "_recovery_receipt" not in scoped.incident_observations.model_dump()
    result["frozen_observations"]["baseline"].clear()
    assert scoped.incident_observations.baseline


def test_missing_optional_evidence_preserves_legacy_context(setup_observations):
    scoped, result = _prepare(setup_observations)
    assert build_rollback_context(scoped) is not None
    assert result["context"] is None
    assert result["verification"]["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("change", ["flag", "serialized", "mutated", "alarm", "evaluation"])
def test_model_flags_or_changed_reader_content_cannot_authorize(compatible, change):
    scoped, result = _prepare(compatible)
    assert result["context"]
    if change == "flag":
        scoped.incident_observations = IncidentObservations(baseline_verified=True)
    elif change == "serialized":
        scoped.incident_observations = IncidentObservations(**scoped.incident_observations.model_dump())
    elif change == "mutated":
        scoped.incident_observations.current["image_digest"] = "sha256:" + "c" * 64
    elif change == "alarm":
        scoped.raw_alarm.alarm_arn += "-different"
    else:
        scoped.raw_alarm.eval_source_metadata = {}
    assert prepare_recovery_evidence(scoped)["context"] is None


@pytest.mark.parametrize(
    "change",
    ["missing", "request", "count", "hash", "source", "runtime", "format", "mapping", "order", "missing_source"],
)
def test_actual_input_counterexamples_fail_early_only(compatible, change):
    _, _, baseline, publish, _, logs, _ = compatible
    receipt = baseline["observations"][-1]
    if change == "missing":
        baseline["observations"].pop()
    elif change == "request":
        receipt["message"]["request_id"] = "c" * 32
    elif change == "count":
        receipt["message"]["count"] = 1
    elif change == "order":
        receipt["timestamp"] += 1000
    elif change == "missing_source":
        baseline["observations"][0]["message"].pop("files")
    else:
        event = logs.filter_log_events.return_value["events"][-1]
        message = json.loads(event["message"])
        if change == "hash":
            message["input_contract_sha256"] = "f" * 64
        else:
            descriptor = message["input_contract"]
            if change == "source":
                descriptor["source_files"]["services/sensor.py"] = "a" * 64
            elif change == "runtime":
                descriptor["runtime"]["pydantic"] = "2.12.0"
            elif change == "format":
                descriptor["format"] = "v2"
            else:
                descriptor["timestamp_mapping"] = "sampled_at"
            message["input_contract_sha256"] = _hash(descriptor)
        event["message"] = json.dumps(message)
    publish()
    scoped, result = _prepare(compatible)
    assert build_rollback_context(scoped) is not None
    assert result["context"] is None
    assert result["verification"]["status"] == "UNAVAILABLE"


def test_deepcopy_preserves_receipt_but_refresh_cannot_launder_model_data(compatible):
    reader, alarm, *_ = compatible
    scoped, _ = _prepare(compatible)
    scoped.incident_observations = scoped.incident_observations.model_copy(deep=True)
    assert prepare_recovery_evidence(scoped)["context"]
    scoped.incident_observations = IncidentObservations(**scoped.incident_observations.model_dump())
    scoped.incident_observations = reader.refresh_current(alarm, scoped.incident_observations, timeout_seconds=90)
    assert prepare_recovery_evidence(scoped)["context"] is None


def test_incomplete_input_pages_are_not_absence_or_positive_proof(compatible):
    *_, logs, _ = compatible
    logs.filter_log_events.return_value["nextToken"] = "repeats"
    scoped, result = _prepare(compatible)
    assert scoped.incident_observations.baseline_verified
    assert result["context"] is None


def test_source_only_evaluation_does_not_read_aws(compatible):
    reader, alarm, _, _, s3, logs, ecs = compatible
    alarm.eval_source_metadata = {}
    observed = reader.observe(alarm, timeout_seconds=90)
    result = prepare_recovery_evidence(SimpleNamespace(raw_alarm=alarm, incident_observations=observed))
    assert result["context"] is None
    s3.get_object.assert_not_called()
    logs.filter_log_events.assert_not_called()
    ecs.describe_services.assert_not_called()
