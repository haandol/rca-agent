"""Versioned inputs preserve sensor identity separately from caller-supplied subject linkage."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from test_service.ports.dto.sensor import ReadingType


@dataclass(frozen=True)
class VitalEvent:
    event_id: str
    sensor_id: str
    patient_id: str
    schema_version: int
    reading_type: str
    value: float
    unit: str
    measured_at: datetime

    def __post_init__(self):
        """Reject missing identity/time and nonfinite values before declaring durable admission."""
        for name, limit in (("event_id", 128), ("sensor_id", 128), ("patient_id", 64), ("unit", 16)):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError("invalid event field")
        if type(self.schema_version) is not int or self.schema_version not in (1, 2):
            raise ValueError("invalid event version")
        if (
            self.reading_type not in ReadingType
            or type(self.value) not in (int, float)
            or not math.isfinite(self.value)
        ):
            raise ValueError("invalid measurement")
        if self.measured_at.tzinfo is None or self.measured_at.utcoffset() is None:
            raise ValueError("measurement time requires timezone")

    def payload(self) -> dict:
        """Canonicalize representation, retaining version and the original measurement instant."""
        return {
            "event_id": self.event_id,
            "sensor_id": self.sensor_id,
            "patient_id": self.patient_id,
            "schema_version": self.schema_version,
            "reading_type": str(self.reading_type),
            "value": float(self.value),
            "unit": self.unit,
            "measured_at": self.measured_at.astimezone(UTC).isoformat(),
        }

    @property
    def digest(self) -> str:
        """Bind duplicate detection to every semantic input field, not only the identifier."""
        return hashlib.sha256(
            json.dumps(self.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    @classmethod
    def from_payload(cls, payload: dict) -> VitalEvent:
        """Reconstruct only the persisted normalized event, never refresh time or identity on retry."""
        return cls(**{**payload, "measured_at": datetime.fromisoformat(payload["measured_at"])})


@dataclass(frozen=True)
class Admission:
    state: str
    duplicate: bool = False
    reading_id: str | None = None


@dataclass(frozen=True)
class AttemptResult:
    state: str
    retried: bool = False
    retry_delay_seconds: float | None = None
    reading_id: str | None = None


class EventConflictError(ValueError):
    """An existing ID denotes different immutable event contents."""


class InboxFullError(RuntimeError):
    """No new event was accepted; existing pending events remain untouched."""


class VitalEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=128)
    sensor_id: str = Field(min_length=1, max_length=128)
    patient_id: str = Field(
        default="P-001",
        min_length=1,
        max_length=64,
        description="Explicit synthetic demo subject by default; never inferred from sensor_id.",
    )
    schema_version: Literal[1, 2]
    reading_type: ReadingType
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=16)
    timestamp: AwareDatetime | None = None
    sampled_at: AwareDatetime | None = None

    @field_validator("value", mode="before")
    @classmethod
    def numeric_measurement(cls, value):
        """A boolean is not a measurement, even if Python could coerce it to a number."""
        if type(value) not in (int, float):
            raise ValueError("invalid measurement")
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_is_integer(cls, value):
        """A boolean or coerced label is not a versioned input contract."""
        if type(value) is not int:
            raise ValueError("invalid version")
        return value

    @model_validator(mode="after")
    def version_matches_time(self):
        """Only the declared version's original time is accepted; do not manufacture a receive-time timestamp."""
        opposite = "sampled_at" if self.schema_version == 1 else "timestamp"
        if opposite in self.model_fields_set:
            raise ValueError("measurement timestamp does not match event version")
        if (self.schema_version == 1 and (self.timestamp is None or self.sampled_at is not None)) or (
            self.schema_version == 2 and (self.sampled_at is None or self.timestamp is not None)
        ):
            raise ValueError("measurement timestamp does not match event version")
        self.event()
        return self

    def event(self) -> VitalEvent:
        """Keep caller-provided patient linkage distinct from sensor identity."""
        return VitalEvent(
            self.event_id,
            self.sensor_id,
            self.patient_id,
            self.schema_version,
            self.reading_type.value,
            self.value,
            self.unit,
            self.timestamp if self.schema_version == 1 else self.sampled_at,
        )


def normalize_vital_event(payload: dict) -> VitalEvent:
    """Normalize the real v1/v2 wire through the same validation for HTTP and generated inputs."""
    return VitalEventInput.model_validate(payload).event()
