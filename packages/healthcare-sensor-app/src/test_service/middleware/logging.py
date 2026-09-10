import logging
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class LoggingMiddleware:
    """Contain HTTP failures before server logging or tracing can expose driver details."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the complete response lifetime so late failures also cross the safe boundary."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Log safe request fields after cleanup and turn unhandled failures into generic 500s.

        Handled HTTP errors keep their responses; cancellation and non-HTTP scopes
        retain their normal lifecycle. Once headers have been sent, abort using a
        fresh, unchained error instead of leaking the original or sending a second
        response. Route templates exclude user path values, queries and headers.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = None
        error_type = None

        async def track_response(message: Message) -> None:
            """Remember the emitted status without buffering bodies or changing backpressure."""
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, track_response)
        except Exception as exc:
            error_type = type(exc).__name__

        # Outside the except block: logging/send failures must not chain driver data.
        extra = {
            "method": scope["method"],
            "path": getattr(scope.get("route"), "path", "<unmatched>"),
            "status_code": status_code if status_code is not None else 500,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
        }
        if error_type is None:
            logger.info("Handled request", extra=extra)
            return
        logger.error("Request failed", extra={**extra, "error_type": error_type})
        if status_code is None:
            response = JSONResponse({"detail": "Internal server error"}, status_code=500)
            await response(scope, receive, send)
            return

        raise RuntimeError("HTTP response failed after headers were sent")
