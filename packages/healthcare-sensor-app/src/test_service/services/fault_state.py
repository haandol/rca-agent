import asyncio
import threading
import weakref
from typing import Protocol


class AsyncClosable(Protocol):
    async def close(self) -> None: ...


class SlowQueryDelay(Protocol):
    _slow_query_ms: int


class EnvironmentDatabaseLeak(Protocol):
    _fault_db_leak: bool


environment_leaked_connections: list[AsyncClosable] = []
_environment_database_leaks: weakref.WeakSet[EnvironmentDatabaseLeak] = weakref.WeakSet()
_environment_database_leaks_disabled = False
_environment_database_leak_acquisitions = 0
_environment_database_leak_condition = threading.Condition()
_slow_query_delays: weakref.WeakSet[SlowQueryDelay] = weakref.WeakSet()


def register_environment_database_leak(leak: EnvironmentDatabaseLeak) -> None:
    with _environment_database_leak_condition:
        _environment_database_leaks.add(leak)
        if _environment_database_leaks_disabled:
            leak._fault_db_leak = False


def begin_environment_database_leak(leak: EnvironmentDatabaseLeak) -> bool:
    global _environment_database_leak_acquisitions
    with _environment_database_leak_condition:
        if _environment_database_leaks_disabled or not leak._fault_db_leak:
            return False
        _environment_database_leak_acquisitions += 1
        return True


def retain_environment_leaked_connection(
    leak: EnvironmentDatabaseLeak,
    connection: AsyncClosable,
) -> bool:
    with _environment_database_leak_condition:
        if _environment_database_leaks_disabled or not leak._fault_db_leak:
            return False
        environment_leaked_connections.append(connection)
        return True


def retain_close_failed_environment_connection(connection: AsyncClosable) -> None:
    with _environment_database_leak_condition:
        environment_leaked_connections.append(connection)


def finish_environment_database_leak() -> None:
    global _environment_database_leak_acquisitions
    with _environment_database_leak_condition:
        _environment_database_leak_acquisitions -= 1
        _environment_database_leak_condition.notify_all()


async def disable_environment_database_leaks() -> None:
    global _environment_database_leaks_disabled
    with _environment_database_leak_condition:
        _environment_database_leaks_disabled = True
        for leak in tuple(_environment_database_leaks):
            leak._fault_db_leak = False


async def wait_for_environment_database_leak_acquisitions() -> None:
    with _environment_database_leak_condition:
        has_pending_acquisitions = _environment_database_leak_acquisitions > 0
    if has_pending_acquisitions:
        await asyncio.to_thread(_wait_for_environment_database_leak_acquisitions)


def _wait_for_environment_database_leak_acquisitions() -> None:
    with _environment_database_leak_condition:
        _environment_database_leak_condition.wait_for(lambda: _environment_database_leak_acquisitions == 0)


async def close_environment_leaked_connections() -> tuple[int, int]:
    with _environment_database_leak_condition:
        connections = list(environment_leaked_connections)
        environment_leaked_connections.clear()

    closed = 0
    failed: list[AsyncClosable] = []
    for connection in connections:
        try:
            await connection.close()
            closed += 1
        except Exception:
            failed.append(connection)

    if failed:
        with _environment_database_leak_condition:
            environment_leaked_connections.extend(failed)
    return closed, len(failed)


def reset_environment_database_leak_state_for_testing() -> None:
    global _environment_database_leaks_disabled
    with _environment_database_leak_condition:
        if _environment_database_leak_acquisitions:
            raise RuntimeError("Cannot reset fault state while leak acquisitions are active")
        _environment_database_leaks_disabled = False
        _environment_database_leaks.clear()
        environment_leaked_connections.clear()


def register_slow_query_delay(delay: SlowQueryDelay) -> None:
    _slow_query_delays.add(delay)


def reset_slow_query_delays() -> None:
    for delay in tuple(_slow_query_delays):
        delay._slow_query_ms = 0
