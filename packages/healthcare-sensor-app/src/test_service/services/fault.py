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
_explicit_database_leak_generation = 0
_explicit_database_leak_acquisitions: dict[int, int] = {}
_explicit_database_leak_resetting = False
_explicit_database_leak_condition = threading.Condition()
_memory_ballast: list[bytes] = []
_cpu_stop_event = threading.Event()
_cpu_threads: list[threading.Thread] = []
_slow_query_stop_event = threading.Event()
_slow_query_thread: threading.Thread | None = None


async def _begin_explicit_database_leak() -> int:
    while True:
        with _explicit_database_leak_condition:
            if not _explicit_database_leak_resetting:
                generation = _explicit_database_leak_generation
                _explicit_database_leak_acquisitions[generation] = (
                    _explicit_database_leak_acquisitions.get(generation, 0) + 1
                )
                return generation
        await asyncio.to_thread(_wait_for_explicit_database_leak_reset)


def _wait_for_explicit_database_leak_reset() -> None:
    with _explicit_database_leak_condition:
        _explicit_database_leak_condition.wait_for(lambda: not _explicit_database_leak_resetting)


def _finish_explicit_database_leak(generation: int) -> None:
    with _explicit_database_leak_condition:
        remaining = _explicit_database_leak_acquisitions[generation] - 1
        if remaining:
            _explicit_database_leak_acquisitions[generation] = remaining
        else:
            del _explicit_database_leak_acquisitions[generation]
        _explicit_database_leak_condition.notify_all()


async def _begin_explicit_database_leak_reset() -> int:
    global _explicit_database_leak_generation, _explicit_database_leak_resetting
    while True:
        with _explicit_database_leak_condition:
            if not _explicit_database_leak_resetting:
                fenced_generation = _explicit_database_leak_generation
                _explicit_database_leak_generation += 1
                _explicit_database_leak_resetting = True
                return fenced_generation
        await asyncio.to_thread(_wait_for_explicit_database_leak_reset)


def _finish_explicit_database_leak_reset() -> None:
    global _explicit_database_leak_resetting
    with _explicit_database_leak_condition:
        _explicit_database_leak_resetting = False
        _explicit_database_leak_condition.notify_all()


async def _wait_for_explicit_database_leak_acquisitions(fenced_generation: int) -> None:
    with _explicit_database_leak_condition:
        has_pending_acquisitions = any(
            generation <= fenced_generation and count
            for generation, count in _explicit_database_leak_acquisitions.items()
        )
    if has_pending_acquisitions:
        await asyncio.to_thread(
            _wait_for_explicit_database_leak_acquisitions_sync,
            fenced_generation,
        )


def _wait_for_explicit_database_leak_acquisitions_sync(fenced_generation: int) -> None:
    with _explicit_database_leak_condition:
        _explicit_database_leak_condition.wait_for(
            lambda: (
                not any(
                    generation <= fenced_generation and count
                    for generation, count in _explicit_database_leak_acquisitions.items()
                )
            )
        )


def _take_explicit_leaked_sessions() -> list[asyncpg.Connection]:
    with _explicit_database_leak_condition:
        sessions = list(_leaked_sessions)
        _leaked_sessions.clear()
        return sessions


def _retain_explicit_leaked_sessions(sessions: list[asyncpg.Connection]) -> None:
    with _explicit_database_leak_condition:
        _leaked_sessions.extend(sessions)


async def _close_explicit_leaked_sessions(
    sessions: list[asyncpg.Connection],
) -> tuple[int, list[asyncpg.Connection]]:
    closed = 0
    failed: list[asyncpg.Connection] = []
    for index, conn in enumerate(sessions):
        try:
            await conn.close()
            closed += 1
        except asyncio.CancelledError:
            _retain_explicit_leaked_sessions([*failed, *sessions[index:]])
            raise
        except Exception:
            failed.append(conn)
            logger.exception("Failed to close intentionally leaked DB connection")
    return closed, failed


class FaultInjectionService:
    def __init__(self, database: DatabasePort) -> None:
        self._database = database

    async def leak_connections(self, count: int) -> dict:
        url = self._database.engine.url
        dsn = f"postgresql://{url.username}:{url.password}@{url.host}:{url.port}/{url.database}"
        generation = await _begin_explicit_database_leak()
        try:
            for _ in range(count):
                conn = await asyncpg.connect(dsn)
                with _explicit_database_leak_condition:
                    _leaked_sessions.append(conn)
                    leaked_total = len(_leaked_sessions)
                logger.warning(
                    "DB connection leaked (intentional fault injection)",
                    extra={"leaked_total": leaked_total},
                )
        finally:
            _finish_explicit_database_leak(generation)

        with _explicit_database_leak_condition:
            leaked_total = len(_leaked_sessions)
        return {
            "leaked_total": leaked_total,
            "pool_checked_out": self._database.checked_out_connections(),
            "pool_size": self._database.pool_size(),
        }

    async def reset_leaked_connections(self) -> dict:
        fenced_generation = await _begin_explicit_database_leak_reset()
        closed = 0
        failed_sessions: list[asyncpg.Connection] = []
        try:
            await disable_environment_database_leaks()

            # Existing leaks must be closed before waiting, otherwise an acquisition
            # blocked by pool exhaustion can never resume and observe the reset.
            sessions = _take_explicit_leaked_sessions()
            explicit_closed, explicit_failed = await _close_explicit_leaked_sessions(sessions)
            closed += explicit_closed
            failed_sessions.extend(explicit_failed)

            environment_closed, _ = await close_environment_leaked_connections()
            closed += environment_closed

            await wait_for_environment_database_leak_acquisitions()
            late_environment_closed, late_environment_failed = await close_environment_leaked_connections()
            closed += late_environment_closed

            await _wait_for_explicit_database_leak_acquisitions(fenced_generation)
            late_sessions = _take_explicit_leaked_sessions()
            late_closed, late_failed = await _close_explicit_leaked_sessions(late_sessions)
            closed += late_closed
            failed_sessions.extend(late_failed)

            _retain_explicit_leaked_sessions(failed_sessions)
            failed = len(failed_sessions) + late_environment_failed
            failed_sessions.clear()
            result = {
                "closed": closed,
                "pool_checked_out": self._database.checked_out_connections(),
            }
            if failed:
                result.update({"status": "failed", "failed": failed})
            return result
        finally:
            if failed_sessions:
                _retain_explicit_leaked_sessions(failed_sessions)
            _finish_explicit_database_leak_reset()

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
        threads = list(_cpu_threads)
        await asyncio.gather(*(asyncio.to_thread(thread.join, 5) for thread in threads))
        remaining_threads = [thread for thread in threads if thread.is_alive()]
        threads_stopped = len(threads) - len(remaining_threads)
        _cpu_threads[:] = remaining_threads
        if remaining_threads:
            return {
                "status": "stop_timeout",
                "threads_stopped": threads_stopped,
                "threads_remaining": len(remaining_threads),
            }
        return {"status": "stopped", "threads_stopped": threads_stopped}

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
