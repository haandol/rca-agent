"""Session lifetime source installed in the r3 image."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


async def close_session(session: AsyncSession) -> None:
    """Finish explicit shutdown cleanup even if its caller is cancelled."""
    cleanup = asyncio.create_task(session.close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


@asynccontextmanager
async def session_scope(factory, owned: set[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Keep service-owned sessions available for shutdown."""
    session = factory()
    owned.add(session)
    try:
        # Only acquired sessions survive a body exception. A failed checkout
        # must not grow an unbounded list of empty sessions.
        await session.connection()
    except BaseException:
        await close_session(session)
        owned.discard(session)
        raise
    yield session
    await session.commit()
    # Regression: this cleanup is only reached after successful execution.
    await close_session(session)
    owned.discard(session)
