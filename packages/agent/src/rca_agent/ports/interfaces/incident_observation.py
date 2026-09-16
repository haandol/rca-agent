from __future__ import annotations

from typing import Protocol

from rca_agent.ports.dto.models import AlarmPayload
from rca_agent.ports.dto.observations import IncidentObservations


class IncidentObservationPort(Protocol):
    def observe(self, alarm: AlarmPayload, *, timeout_seconds: float) -> IncidentObservations:
        """Read a bounded incident scope without supplying a diagnosis or execution instructions."""
        ...
