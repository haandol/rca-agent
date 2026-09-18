"""Exercise real Vital HTTP/DTO/service boundaries, not PostgreSQL durability."""

import importlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from test_service import telemetry
from test_service.adapters.primary.vital_controller import VitalEventCreate
from test_service.di.app_container import AppContainer
from test_service.ports.dto.vital import Admission, EventConflictError, InboxFullError, VitalEvent
from test_service.services.vital import VitalService

PRIVATE = "PRIVATE_PATIENT_VALUE_9127"
TIME = "2026-09-18T09:12:34.567890+09:00"


def event_body(version=2):
    """Keep sensor identity and optional synthetic patient linkage distinct."""
    return dict(
        event_id="event-api-1",
        sensor_id="sensor-heart-9",
        patient_id="demo-person-3",
        schema_version=version,
        reading_type="heart_rate",
        value=73.25,
        unit="bpm",
        **{"timestamp" if version == 1 else "sampled_at": TIME},
    )


class RecordingRepository:
    """Return configured storage outcomes without claiming a real commit occurred."""

    def __init__(self):
        self.calls = []
        self.result = Admission("PENDING")

    async def admit(self, event):
        self.calls.append(event)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
async def vital_api(monkeypatch):
    """Use production routes, middleware and privacy handler without lifespan/cloud work."""
    with (
        patch.object(telemetry, "setup_logging"),
        patch.object(telemetry, "setup_telemetry"),
        patch.object(AppContainer, "create_router", return_value=APIRouter()),
    ):
        main = importlib.import_module("test_service.main")
    repository = RecordingRepository()
    container = AppContainer()
    container._vital_service = VitalService(repository, None)
    container._sensor_service = SimpleNamespace()
    container._health_service = SimpleNamespace()
    monkeypatch.setattr(main, "container", container)
    monkeypatch.setattr(main, "setup_logging", lambda _: None)
    monkeypatch.setattr(main, "setup_telemetry", lambda *_: None)
    async with AsyncClient(transport=ASGITransport(app=main.create_app()), base_url="http://test") as client:
        yield client, repository


@pytest.mark.parametrize("version", [1, 2])
async def test_versioned_time_reaches_repository_unchanged(vital_api, version):
    """Both wire versions normalize the original measurement instant, never receive time."""
    client, repository = vital_api
    response = await client.post("/sensors/events", json=event_body(version))
    assert response.status_code == 202
    assert response.json() == dict(event_id="event-api-1", state="PENDING", duplicate=False, measurement_id=None)
    [event] = repository.calls
    assert isinstance(event, VitalEvent)
    assert event.payload() == dict(
        event_id="event-api-1",
        sensor_id="sensor-heart-9",
        patient_id="demo-person-3",
        schema_version=version,
        reading_type="heart_rate",
        value=73.25,
        unit="bpm",
        measured_at="2026-09-18T00:12:34.567890+00:00",
    )
    assert event.measured_at == datetime.fromisoformat(TIME)
    assert VitalEvent.from_payload(event.payload()) == event


async def test_optional_patient_is_explicit_demo_default(vital_api):
    """Omitted patient linkage cannot silently copy the sensor identifier."""
    client, repository = vital_api
    body = event_body()
    del body["patient_id"]
    assert (await client.post("/sensors/events", json=body)).status_code == 202
    assert repository.calls[0].patient_id == "P-001"
    assert repository.calls[0].patient_id != repository.calls[0].sensor_id


INVALID = [
    ({"schema_version": 3}, ()),
    ({"schema_version": True}, ()),
    ({"schema_version": "2"}, ()),
    ({"schema_version": 2.0}, ()),
    ({}, ("sampled_at",)),
    ({"sampled_at": None}, ()),
    ({"timestamp": TIME}, ()),
    ({"timestamp": None}, ()),
    ({"schema_version": 1}, ()),
    ({"schema_version": 1, "timestamp": TIME}, ()),
    ({"schema_version": 1, "timestamp": TIME, "sampled_at": None}, ()),
    ({"sampled_at": "2026-09-18T00:00:00"}, ()),
    ({"value": True}, ()),
    ({"value": "73.25"}, ()),
    ({"value": float("nan")}, ()),
    ({"value": float("inf")}, ()),
    ({"value": float("-inf")}, ()),
    ({"sensor_id": " "}, ()),
    ({"patient_id": None}, ()),
    ({PRIVATE: {"secret": PRIVATE}}, ()),
    ({"reading_type": PRIVATE}, ()),
    ({"sampled_at": PRIVATE}, ()),
]


@pytest.mark.parametrize("updates,removed", INVALID)
async def test_invalid_inputs_are_private_and_never_admitted(vital_api, caplog, updates, removed):
    """Unknown field names and rejected patient values must not escape the actual 422 handler."""
    client, repository = vital_api
    body = {**event_body(), "patient_id": PRIVATE, **updates}
    for field in removed:
        del body[field]
    response = await client.post(
        "/sensors/events", content=json.dumps(body), headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert repository.calls == []
    details = response.json()["detail"]
    assert details
    assert all(set(item) == {"type", "loc", "msg"} for item in details)
    assert all(item["msg"] in {"Field required", "Invalid input"} for item in details)
    assert PRIVATE not in response.text
    assert PRIVATE not in json.dumps([r.__dict__ for r in caplog.records], default=str)


@pytest.mark.parametrize(
    "receipt,status,expected",
    [
        (
            Admission("PENDING", True),
            202,
            dict(event_id="event-api-1", state="PENDING", duplicate=True, measurement_id=None),
        ),
        (
            Admission("COMPLETED", True, "reading-1"),
            200,
            dict(event_id="event-api-1", state="COMPLETED", duplicate=True, measurement_id="reading-1"),
        ),
        (EventConflictError(PRIVATE), 409, {"code": "EVENT_ID_CONFLICT"}),
        (InboxFullError(PRIVATE), 503, {"code": "INBOX_FULL", "accepted": False}),
        (RuntimeError(PRIVATE), 503, {"code": "ADMISSION_UNCONFIRMED", "retry_same_event_id": True}),
    ],
)
async def test_storage_outcomes_remain_distinct(vital_api, caplog, receipt, status, expected):
    """Only known completion returns 200; an uncertain commit cannot claim non-admission."""
    client, repository = vital_api
    repository.result = receipt
    response = await client.post("/sensors/events", json=event_body())
    assert response.status_code == status
    assert response.json() == expected
    assert len(repository.calls) == 1
    assert PRIVATE not in response.text
    assert PRIVATE not in json.dumps([r.__dict__ for r in caplog.records], default=str)


async def test_commit_unknown_retry_preserves_input(vital_api):
    """Retransmission keeps the event ID, measurement time and conflict fingerprint fixed."""
    client, repository = vital_api
    repository.result = RuntimeError("commit acknowledgement lost")
    assert (await client.post("/sensors/events", json=event_body())).status_code == 503
    repository.result = Admission("COMPLETED", True, "reading-existing")
    response = await client.post("/sensors/events", json=event_body())
    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert repository.calls[0] == repository.calls[1]
    assert repository.calls[0].digest == repository.calls[1].digest


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "other-event"),
        ("sensor_id", "other-sensor"),
        ("patient_id", "other-patient"),
        ("schema_version", 1),
        ("reading_type", "temperature"),
        ("value", 74),
        ("unit", "other"),
        ("measured_at", datetime(2026, 9, 19, tzinfo=UTC)),
    ],
)
def test_dto_digest_binds_every_semantic_field(field, value):
    """Same-ID conflict detection must include every immutable measurement field."""
    event = VitalEventCreate.model_validate(event_body()).event()
    assert replace(event, **{field: value}).digest != event.digest


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 3),
        ("value", False),
        ("value", float("nan")),
        ("value", float("inf")),
        ("sensor_id", " "),
        ("measured_at", datetime(2026, 9, 18)),
    ],
)
def test_dto_rejects_invalid_non_http_input(field, value):
    """Internal callers cannot bypass finite value, version and aware-time requirements."""
    event = VitalEventCreate.model_validate(event_body()).event()
    with pytest.raises(ValueError):
        replace(event, **{field: value})
