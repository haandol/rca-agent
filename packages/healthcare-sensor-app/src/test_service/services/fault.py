import asyncio
import logging
import threading

import asyncpg

from test_service.ports.interfaces.database import DatabasePort
from test_service.services.fault_state import (
    close_environment_leaked_connections,
    disable_environment_database_leaks,
    reset_slow_query_delays,
    wait_for_environment_database_leak_acquisitions,
)

logger = logging.getLogger(__name__)

_leaked_sessions: list[asyncpg.Connection] = []
_memory_ballast: list[bytes] = []
_cpu_stop_event = threading.Event()
_cpu_threads: list[threading.Thread] = []
_slow_query_stop_event = threading.Event()
_slow_query_thread: threading.Thread | None = None


class FaultInjectionService:
    def __init__(self, database: DatabasePort) -> None:
        self._database = database

    async def leak_connections(self, count: int) -> dict:
        url = self._database.engine.url
        dsn = f"postgresql://{url.username}:{url.password}@{url.host}:{url.port}/{url.database}"
        for _ in range(count):
            conn = await asyncpg.connect(dsn)
            _leaked_sessions.append(conn)
            logger.warning(
                "DB connection leaked (intentional fault injection)",
                extra={"leaked_total": len(_leaked_sessions)},
            )

        return {
            "leaked_total": len(_leaked_sessions),
            "pool_checked_out": self._database.checked_out_connections(),
            "pool_size": self._database.pool_size(),
        }

    async def reset_leaked_connections(self) -> dict:
        await disable_environment_database_leaks()
        closed = 0
        failed_sessions: list[asyncpg.Connection] = []
        sessions = list(_leaked_sessions)
        _leaked_sessions.clear()
        for conn in sessions:
            try:
                await conn.close()
                closed += 1
            except Exception:
                failed_sessions.append(conn)
                logger.exception("Failed to close intentionally leaked DB connection")
        _leaked_sessions.extend(failed_sessions)

        environment_closed, _ = await close_environment_leaked_connections()
        closed += environment_closed

        # Existing leaks must be closed before waiting, otherwise an acquisition
        # blocked by pool exhaustion can never resume and observe the reset.
        await wait_for_environment_database_leak_acquisitions()
        late_closed, late_failed = await close_environment_leaked_connections()
        closed += late_closed
        failed = len(failed_sessions) + late_failed
        result = {"closed": closed, "pool_checked_out": self._database.checked_out_connections()}
        if failed:
            result.update({"status": "failed", "failed": failed})
        return result

    def start_high_cpu(self) -> dict:
        global _cpu_stop_event
        if _cpu_threads:
            return {"status": "already_running", "threads": len(_cpu_threads)}

        _cpu_stop_event.clear()

        def _burn_cpu(stop: threading.Event):
            while not stop.is_set():
                _ = sum(i * i for i in range(10_000))
            logger.error("High CPU fault injection stopped")

        import os

        num_threads = os.cpu_count() or 1
        for _ in range(num_threads):
            t = threading.Thread(target=_burn_cpu, args=(_cpu_stop_event,), daemon=True)
            t.start()
            _cpu_threads.append(t)

        logger.error("High CPU fault injection started", extra={"threads": num_threads})
        return {"status": "started", "threads": num_threads}

    async def stop_high_cpu(self) -> dict:
        if not _cpu_threads:
            return {"status": "not_running"}
        _cpu_stop_event.set()
        await asyncio.gather(*(asyncio.to_thread(t.join, 5) for t in _cpu_threads))
        count = len(_cpu_threads)
        _cpu_threads.clear()
        return {"status": "stopped", "threads_stopped": count}

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

    def start_slow_query(self, seconds: int) -> dict:
        global _slow_query_thread
        if _slow_query_thread and _slow_query_thread.is_alive():
            return {"status": "already_running"}

        _slow_query_stop_event.clear()
        self._slow_query_interval = seconds

        def _repeat_slow_query(stop: threading.Event, db: DatabasePort, interval: int):
            import asyncio

            from sqlalchemy import text

            loop = asyncio.new_event_loop()

            async def _run():
                while not stop.is_set():
                    try:
                        async for session in db.session():
                            await session.execute(text(f"SELECT pg_sleep({interval})"))
                    except Exception:
                        pass
                logger.error("Slow query fault injection stopped")

            loop.run_until_complete(_run())
            loop.close()

        _slow_query_thread = threading.Thread(
            target=_repeat_slow_query,
            args=(_slow_query_stop_event, self._database, seconds),
            daemon=True,
        )
        _slow_query_thread.start()
        logger.error("Slow query fault injection started", extra={"interval_seconds": seconds})
        return {"status": "started", "interval_seconds": seconds}

    async def stop_slow_query(self) -> dict:
        global _slow_query_thread
        reset_slow_query_delays()
        if not _slow_query_thread:
            return {"status": "not_running"}
        if not _slow_query_thread.is_alive():
            _slow_query_thread = None
            return {"status": "not_running"}
        _slow_query_stop_event.set()
        await asyncio.to_thread(_slow_query_thread.join, 35)
        if _slow_query_thread.is_alive():
            return {"status": "stop_timeout"}
        _slow_query_thread = None
        return {"status": "stopped"}
