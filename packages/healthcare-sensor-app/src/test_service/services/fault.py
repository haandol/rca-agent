import logging
import threading

from test_service.ports.interfaces.database import DatabasePort

logger = logging.getLogger(__name__)

_leaked_sessions: list = []
_memory_ballast: list[bytes] = []


class FaultInjectionService:
    def __init__(self, database: DatabasePort) -> None:
        self._database = database

    async def leak_connections(self, count: int) -> dict:
        for _ in range(count):
            async for session in self._database.leaky_session():
                _leaked_sessions.append(session)
            checked = self._database.checked_out_connections()
            logger.warning("DB connection leaked (intentional fault injection)", extra={"pool_checked_out": checked})

        return {
            "leaked_total": len(_leaked_sessions),
            "pool_checked_out": self._database.checked_out_connections(),
            "pool_size": self._database.pool_size(),
        }

    async def reset_leaked_connections(self) -> dict:
        closed = 0
        for session in _leaked_sessions:
            try:
                await session.close()
                closed += 1
            except Exception:
                pass
        _leaked_sessions.clear()
        return {"closed": closed, "pool_checked_out": self._database.checked_out_connections()}

    def start_high_cpu(self, seconds: int) -> dict:
        def _burn_cpu(duration: int):
            import time

            end = time.monotonic() + duration
            while time.monotonic() < end:
                _ = sum(i * i for i in range(10_000))
            logger.error("High CPU fault injection completed", extra={"duration_seconds": duration})

        t = threading.Thread(target=_burn_cpu, args=(seconds,), daemon=True)
        t.start()
        logger.error("High CPU fault injection started", extra={"duration_seconds": seconds})
        return {"status": "started", "duration_seconds": seconds}

    def allocate_memory(self, megabytes: int) -> dict:
        ballast = b"\x00" * (megabytes * 1024 * 1024)
        _memory_ballast.append(ballast)
        total = len(_memory_ballast)
        logger.error("High memory fault injection", extra={"allocated_mb": megabytes, "total_ballasts": total})
        return {"allocated_mb": megabytes, "total_ballasts": total}

    def release_memory(self) -> dict:
        count = len(_memory_ballast)
        _memory_ballast.clear()
        return {"released_ballasts": count}

    async def slow_query(self, seconds: int) -> dict:
        from sqlalchemy import text

        async for session in self._database.session():
            await session.execute(text(f"SELECT pg_sleep({seconds})"))

        logger.error("Slow query fault injection completed", extra={"duration_seconds": seconds})
        return {"status": "completed", "duration_seconds": seconds}
