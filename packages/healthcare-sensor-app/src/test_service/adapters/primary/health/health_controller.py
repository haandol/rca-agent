from fastapi import APIRouter

from test_service.adapters.primary.schemas import HealthResponse
from test_service.services.health import HealthService


class HealthController:
    def __init__(self, service: HealthService) -> None:
        self._service = service
        self.router = APIRouter(tags=["health"])
        self.router.add_api_route("/healthz", self.healthcheck, methods=["GET"], response_model=HealthResponse)

    async def healthcheck(self) -> HealthResponse:
        result = await self._service.check()
        return HealthResponse(**result)
