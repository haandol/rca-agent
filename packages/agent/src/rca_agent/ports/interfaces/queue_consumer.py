from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator


class QueueConsumerPort(ABC):
    @abstractmethod
    def poll(self) -> Iterator[tuple[dict, str]]:
        """Yield (message_body, receipt_handle) tuples."""
        ...

    @abstractmethod
    def ack(self, receipt_handle: str) -> None: ...
