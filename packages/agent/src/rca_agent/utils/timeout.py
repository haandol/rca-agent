from __future__ import annotations

import os
import signal
import threading
from collections.abc import Callable


class _OperationTimeout(BaseException):
    def __init__(self, token: object) -> None:
        self.token = token


def call_with_timeout[T](operation: Callable[[], T], timeout_seconds: float) -> T:
    if timeout_seconds <= 0:
        raise TimeoutError("operation timed out")
    if (
        os.name != "posix"
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
    ):
        raise RuntimeError("hard timeouts require a POSIX main thread")

    active_delay, _ = signal.getitimer(signal.ITIMER_REAL)
    if active_delay > 0:
        raise RuntimeError("hard timeout unavailable while ITIMER_REAL is active")

    token = object()
    previous_handler = signal.getsignal(signal.SIGALRM)

    def raise_timeout(signum, frame) -> None:  # noqa: ARG001
        raise _OperationTimeout(token)

    try:
        signal.signal(signal.SIGALRM, raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        try:
            return operation()
        except _OperationTimeout as exc:
            if exc.token is not token:
                raise
            raise TimeoutError("operation timed out") from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
