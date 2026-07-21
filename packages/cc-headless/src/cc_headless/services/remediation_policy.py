from __future__ import annotations

from enum import StrEnum


class HealthcareFaultType(StrEnum):
    DB_LEAK = "db-leak"
    HIGH_CPU = "high-cpu"
    HIGH_MEMORY = "high-memory"
    SLOW_QUERY = "slow-query"
    UNSUPPORTED = "unsupported"


RESET_PATHS: dict[HealthcareFaultType, str] = {
    HealthcareFaultType.DB_LEAK: "/fault/db-leak/reset",
    HealthcareFaultType.HIGH_CPU: "/fault/high-cpu/reset",
    HealthcareFaultType.HIGH_MEMORY: "/fault/high-memory/reset",
    HealthcareFaultType.SLOW_QUERY: "/fault/slow-query/reset",
}


def parse_fault_type(value: object) -> HealthcareFaultType | None:
    if not isinstance(value, str):
        return None
    try:
        return HealthcareFaultType(value)
    except ValueError:
        return None
