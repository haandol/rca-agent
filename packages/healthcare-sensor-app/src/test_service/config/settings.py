from dataclasses import dataclass
from functools import lru_cache
from os import environ


@dataclass(frozen=True)
class AppSettings:
    database_url: str
    otel_exporter_otlp_endpoint: str
    otel_service_name: str
    log_level: str
    fault_injection_enabled: bool
    db_pool_size: int
    db_max_overflow: int


@lru_cache(1)
def get_settings() -> AppSettings:
    return AppSettings(
        database_url=environ.get("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test_service"),
        otel_exporter_otlp_endpoint=environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
        otel_service_name=environ.get("OTEL_SERVICE_NAME", "healthcare-sensor-app"),
        log_level=environ.get("LOG_LEVEL", "INFO"),
        fault_injection_enabled=environ.get("FAULT_INJECTION_ENABLED", "true").lower() == "true",
        db_pool_size=int(environ.get("DB_POOL_SIZE", "5")),
        db_max_overflow=int(environ.get("DB_MAX_OVERFLOW", "10")),
    )
