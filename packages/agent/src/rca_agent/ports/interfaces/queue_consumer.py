from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator


class QueueConsumerPort(ABC):
    @abstractmethod
    def poll(self) -> Iterator[tuple[dict, str, int, str]]:
        """Yield (message_body, receipt_handle, receive_count, message_id) tuples."""
        ...

    @abstractmethod
    def ack(self, receipt_handle: str) -> None: ...
