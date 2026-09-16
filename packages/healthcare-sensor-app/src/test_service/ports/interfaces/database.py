from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


class DatabasePort(ABC):
    @asynccontextmanager
    async def session_context(self):
        """Forward body exceptions into session generators and close promptly.

        New adapters override this with a native async context manager. Keeping
        the bridge lets existing test adapters and external callers migrate.
        """
        factory = self.session
        async with asynccontextmanager(factory)() as session:
            yield session

    @abstractmethod
    def session(self) -> AsyncGenerator[AsyncSession]:
        """Keep iterator compatibility; consumers needing body-error propagation use session_context."""
        ...

    @abstractmethod
    def checked_out_connections(self) -> int:
        """Report borrowed application connections for pool-pressure diagnosis."""
        ...

    @abstractmethod
    def pool_size(self) -> int:
        """Report persistent application-pool capacity rather than the database-wide limit."""
        ...

    @abstractmethod
    async def dispose(self) -> None:
        """Release adapter-owned resources after callers have stopped submitting work."""
        ...
