from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import NamedTuple


class QueueMessage(NamedTuple):
    """Keep the current receipt's receive-request clock with its existing delivery identity."""

    body: dict
    receipt_handle: str
    receive_count: int
    message_id: str
    received_at_monotonic: float


class QueueReceiveError(RuntimeError):
    """The queue could not be read; the polling loop should wait before retrying."""


class QueueLeaseLostError(RuntimeError):
    """Receipt ownership is uncertain; do not start work, publish results or acknowledge it."""


class QueueConsumerPort(ABC):
    @abstractmethod
    def poll(self) -> Iterator[QueueMessage]:
        """Yield identity and a conservative clock taken before this ReceiveMessage request.

        Raise QueueReceiveError when the queue cannot be read.
        """
        ...

    @abstractmethod
    def ack(self, receipt_handle: str) -> None: ...

    @abstractmethod
    def renew_visibility(self, receipt_handle: str, visibility_seconds: int) -> None:
        """Confirm visibility for this receipt or raise; never silently accept uncertain renewal."""
        ...
