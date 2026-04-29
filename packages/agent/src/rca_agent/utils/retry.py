from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 1.0


def retry_with_backoff[T](
    fn: Callable[[], T],
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    operation: str = "operation",
    default: T | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T | None:
    """Run ``fn`` up to ``max_retries`` times with exponential backoff on exception.

    Returns ``fn()`` on success, ``default`` after exhausting retries. On the
    final failure, logs with ``logger.exception``; on intermediate failures,
    logs a warning and sleeps ``base_delay * 2**attempt`` seconds.
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception:
            if attempt == max_retries - 1:
                logger.exception("Failed %s after %d attempts", operation, max_retries)
                return default
            delay = base_delay * (2**attempt)
            logger.warning(
                "%s attempt %d failed, retrying in %.1fs",
                operation.capitalize(),
                attempt + 1,
                delay,
            )
            sleep(delay)
    return default
