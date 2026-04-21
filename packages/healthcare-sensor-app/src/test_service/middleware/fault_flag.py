import asyncio
import logging
import random

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from test_service.config import AppSettings

logger = logging.getLogger(__name__)

_PASSTHROUGH_PREFIXES = ("/healthz", "/fault", "/docs", "/openapi.json")


class FaultFlagMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: AppSettings) -> None:
        super().__init__(app)
        self._error_rate = settings.fault_error_rate
        self._slow_query_ms = settings.fault_slow_query_ms

    async def dispatch(self, request: Request, call_next) -> Response:
        if any(request.url.path.startswith(p) for p in _PASSTHROUGH_PREFIXES):
            return await call_next(request)

        if self._error_rate > 0 and random.random() < self._error_rate:
            logger.error(
                "Request rejected by FAULT_ERROR_RATE",
                extra={"path": request.url.path, "error_rate": self._error_rate},
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error"},
            )

        if self._slow_query_ms > 0:
            delay = self._slow_query_ms / 1000.0
            logger.warning(
                "Injecting latency via FAULT_SLOW_QUERY_MS",
                extra={"path": request.url.path, "delay_ms": self._slow_query_ms},
            )
            await asyncio.sleep(delay)

        return await call_next(request)
