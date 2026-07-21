from __future__ import annotations

from collections.abc import Callable
from queue import Empty, Queue
from threading import Thread


def call_with_timeout[T](operation: Callable[[], T], timeout_seconds: float) -> T:
    result: Queue[tuple[bool, T | BaseException]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put((True, operation()))
        except BaseException as exc:
            result.put((False, exc))

    Thread(target=invoke, daemon=True).start()
    try:
        succeeded, value = result.get(timeout=max(0, timeout_seconds))
    except Empty as exc:
        raise TimeoutError("operation timed out") from exc

    if not succeeded:
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("operation failed without an exception")
    return value
