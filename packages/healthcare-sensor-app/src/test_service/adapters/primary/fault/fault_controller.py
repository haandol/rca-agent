from fastapi import APIRouter, HTTPException

from test_service.adapters.primary.schemas import (
    FaultDurationRequest,
    FaultMemoryRequest,
    FaultRequest,
    FaultSlowQueryRequest,
)
from test_service.config import AppSettings
from test_service.services.fault import FaultInjectionService


class FaultController:
    def __init__(self, service: FaultInjectionService, settings: AppSettings) -> None:
        self._service = service
        self._enabled = settings.fault_injection_enabled
        self.router = APIRouter(prefix="/fault", tags=["fault"])
        self.router.add_api_route("/db-leak", self.db_leak, methods=["POST"])
        self.router.add_api_route("/db-leak/reset", self.db_leak_reset, methods=["POST"])
        self.router.add_api_route("/high-cpu", self.high_cpu, methods=["POST"])
        self.router.add_api_route("/high-memory", self.high_memory, methods=["POST"])
        self.router.add_api_route("/high-memory/reset", self.high_memory_reset, methods=["POST"])
        self.router.add_api_route("/slow-query", self.slow_query, methods=["POST"])

    def _require_enabled(self):
        if not self._enabled:
            raise HTTPException(status_code=403, detail="Fault injection is disabled")

    async def db_leak(self, req: FaultRequest):
        self._require_enabled()
        return await self._service.leak_connections(req.count)

    async def db_leak_reset(self):
        self._require_enabled()
        return await self._service.reset_leaked_connections()

    async def high_cpu(self, req: FaultDurationRequest):
        self._require_enabled()
        return self._service.start_high_cpu(req.seconds)

    async def high_memory(self, req: FaultMemoryRequest):
        self._require_enabled()
        return self._service.allocate_memory(req.megabytes)

    async def high_memory_reset(self):
        self._require_enabled()
        return self._service.release_memory()

    async def slow_query(self, req: FaultSlowQueryRequest):
        self._require_enabled()
        return await self._service.slow_query(req.seconds)
