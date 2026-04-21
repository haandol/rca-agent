from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession


class DatabasePort(ABC):
    @abstractmethod
    def session(self) -> AsyncGenerator[AsyncSession]: ...

    @abstractmethod
    def leaky_session(self) -> AsyncGenerator[AsyncSession]:
        """Return a session that intentionally leaks — for fault injection only."""
        ...

    @abstractmethod
    def checked_out_connections(self) -> int: ...

    @abstractmethod
    def pool_size(self) -> int: ...

    @abstractmethod
    async def dispose(self) -> None: ...
