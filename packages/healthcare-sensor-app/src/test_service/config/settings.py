from dataclasses import dataclass
from functools import lru_cache
from math import isfinite
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
    fault_db_leak: bool
    fault_slow_query_ms: int
    fault_error_rate: float
    deployed_revision: str
    traffic_enabled: bool = True
    traffic_interval_seconds: float = 5.0
    traffic_max_concurrency: int = 1
    traffic_query_limit: int = 20
    traffic_patient_id: str | None = None
    traffic_seed: int | None = None
    metric_flush_interval_seconds: float = 30.0
    db_pool_timeout_seconds: float = 30.0
    db_statement_timeout_ms: int = 0
    db_observability_enabled: bool = False
    db_observability_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        """Reject tuning that would create busy loops or unbounded/invalid work.

        Existing required fields remain unchanged; new settings have defaults so
        callers constructing settings directly retain their previous API.
        Pool capacity must stay finite: require a positive base size and
        nonnegative overflow before constructing the database adapter.
        """
        for name, minimum in (("db_pool_size", 1), ("db_max_overflow", 0)):
            value = getattr(self, name)
            if not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name in (
            "traffic_interval_seconds",
            "metric_flush_interval_seconds",
            "db_pool_timeout_seconds",
            "db_observability_interval_seconds",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("traffic_max_concurrency", "traffic_query_limit"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.db_statement_timeout_ms < 0:
            raise ValueError("db_statement_timeout_ms must be nonnegative")


def _build_database_url() -> str:
    if url := environ.get("DATABASE_URL"):
        return url
    user = environ.get("DB_USERNAME", "postgres")
    password = environ.get("DB_PASSWORD", "postgres")
    host = environ.get("DB_HOST", "localhost")
    port = environ.get("DB_PORT", "5432")
    name = environ.get("DB_NAME", "test_service")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


@lru_cache(1)
def get_settings() -> AppSettings:
    """Load bounded workload and observation tuning without changing legacy defaults."""
    return AppSettings(
        database_url=_build_database_url(),
        otel_exporter_otlp_endpoint=environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
        otel_service_name=environ.get("OTEL_SERVICE_NAME", "healthcare-sensor-app"),
        log_level=environ.get("LOG_LEVEL", "INFO"),
        fault_injection_enabled=environ.get("FAULT_INJECTION_ENABLED", "true").lower() == "true",
        db_pool_size=int(environ.get("DB_POOL_SIZE", "5")),
        db_max_overflow=int(environ.get("DB_MAX_OVERFLOW", "10")),
        fault_db_leak=environ.get("FAULT_DB_LEAK", "false").lower() == "true",
        fault_slow_query_ms=int(environ.get("FAULT_SLOW_QUERY_MS", "0")),
        fault_error_rate=float(environ.get("FAULT_ERROR_RATE", "0.0")),
        deployed_revision=environ.get("DEPLOYED_REVISION", "unknown"),
        traffic_enabled=environ.get("TRAFFIC_ENABLED", "true").lower() == "true",
        traffic_interval_seconds=float(environ.get("TRAFFIC_INTERVAL_SECONDS", "5")),
        traffic_max_concurrency=int(environ.get("TRAFFIC_MAX_CONCURRENCY", "1")),
        traffic_query_limit=int(environ.get("TRAFFIC_QUERY_LIMIT", "20")),
        traffic_patient_id=environ.get("TRAFFIC_PATIENT_ID") or None,
        traffic_seed=int(seed) if (seed := environ.get("TRAFFIC_SEED")) else None,
        metric_flush_interval_seconds=float(environ.get("METRIC_FLUSH_INTERVAL_SECONDS", "30")),
        db_pool_timeout_seconds=float(environ.get("DB_POOL_TIMEOUT_SECONDS", "30")),
        db_statement_timeout_ms=int(environ.get("DB_STATEMENT_TIMEOUT_MS", "0")),
        db_observability_enabled=environ.get("DB_OBSERVABILITY_ENABLED", "false").lower() == "true",
        db_observability_interval_seconds=float(environ.get("DB_OBSERVABILITY_INTERVAL_SECONDS", "5")),
    )
