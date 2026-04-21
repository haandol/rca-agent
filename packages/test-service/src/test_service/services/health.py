import time

from test_service.ports.interfaces.database import DatabasePort

_start_time = time.monotonic()


class HealthService:
    def __init__(self, database: DatabasePort) -> None:
        self._database = database

    async def check(self) -> dict:
        db_connected = False
        active_connections = 0

        try:
            from sqlalchemy import text

            async for session in self._database.session():
                await session.execute(text("SELECT 1"))
            db_connected = True
            active_connections = self._database.checked_out_connections()
        except Exception:
            pass

        return {
            "status": "ok" if db_connected else "degraded",
            "db_connected": db_connected,
            "active_db_connections": active_connections,
            "uptime_seconds": round(time.monotonic() - _start_time, 2),
        }
