"""Versioned admission returns durable receipt state, not a fabricated measurement response."""

from fastapi import APIRouter
from starlette.responses import JSONResponse

from test_service.ports.dto.vital import EventConflictError, InboxFullError
from test_service.ports.dto.vital import VitalEventInput as VitalEventCreate


class VitalController:
    def __init__(self, service_factory):
        """Resolve the durable service only for versioned admission, preserving existing legacy query routes."""
        self._service_factory = service_factory
        self.router = APIRouter(prefix="/sensors", tags=["Vital Sensor"])
        self.router.add_api_route("/events", self.admit, methods=["POST"])
        self.router.add_api_route("/events/checkpoint", self.checkpoint, methods=["GET"])
        self.router.add_api_route("/events/cohort", self.cohort, methods=["GET"])

    async def admit(self, event: VitalEventCreate):
        """Return 409 for immutable conflicts and distinguish unconfirmed writes from known non-admission."""
        try:
            receipt = await self._service_factory().admit(event.event())
        except EventConflictError:
            return JSONResponse(status_code=409, content={"code": "EVENT_ID_CONFLICT"})
        except InboxFullError:
            return JSONResponse(status_code=503, content={"code": "INBOX_FULL", "accepted": False})
        except Exception:
            return JSONResponse(status_code=503, content={"code": "ADMISSION_UNCONFIRMED", "retry_same_event_id": True})
        return JSONResponse(
            status_code=200 if receipt.state == "COMPLETED" else 202,
            content={
                "event_id": event.event_id,
                "state": receipt.state,
                "duplicate": receipt.duplicate,
                "measurement_id": receipt.reading_id,
            },
        )

    async def checkpoint(self):
        """Expose a consistent cohort boundary, never any pending measurement contents."""
        try:
            return await self._service_factory().checkpoint()
        except Exception:
            return JSONResponse(status_code=503, content={"code": "COHORT_UNAVAILABLE"})

    async def cohort(self, epoch: str, lower_exclusive: int, upper_inclusive: int):
        """Return verification counts for a fixed epoch/range; missing data cannot imply success."""
        if not 0 <= lower_exclusive <= upper_inclusive:
            return JSONResponse(status_code=422, content={"code": "INVALID_COHORT_RANGE"})
        try:
            return await self._service_factory().cohort(epoch, lower_exclusive, upper_inclusive)
        except Exception:
            return JSONResponse(status_code=503, content={"code": "COHORT_UNAVAILABLE"})
