from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator


class QueueReceiveError(RuntimeError):
    """The queue could not be read; the polling loop should wait before retrying."""


class QueueConsumerPort(ABC):
    @abstractmethod
    def poll(self) -> Iterator[tuple[dict, str, int, str]]:
        """Yield (body, receipt_handle, receive_count, message_id).

        Raise QueueReceiveError when the queue cannot be read.
        """
        ...

    @abstractmethod
    def ack(self, receipt_handle: str) -> None: ...
