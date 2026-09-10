"""Stable session lifetime; exceptions and cancellation reach this scope."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


async def close_session(session: AsyncSession) -> None:
    """Finish closing even if the caller is cancelled during cleanup."""
    cleanup = asyncio.create_task(session.close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


@asynccontextmanager
async def session_scope(factory, owned: set[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Commit success and return the connection on every exit."""
    session = factory()
    owned.add(session)
    try:
        yield session
        await session.commit()
    finally:
        # close also rolls back an unfinished transaction, including DB errors.
        await close_session(session)
        owned.discard(session)
