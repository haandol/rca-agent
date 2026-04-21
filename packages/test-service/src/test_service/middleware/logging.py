import logging
import time
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            extra = {
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            }
            logger.info("Handled request", extra=extra)
            return response
        except Exception:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            extra = {"method": request.method, "path": request.url.path, "elapsed_ms": elapsed_ms}
            logger.exception("Request failed", extra=extra)
            raise
