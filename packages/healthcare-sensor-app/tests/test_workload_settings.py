"""Settings compatibility and alarm-facing metric wiring."""

from dataclasses import replace

import pytest

from test_service.config import settings as settings_module
from test_service.di.app_container import AppContainer


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Keep host tuning and the cached settings singleton out of configuration assertions."""
    monkeypatch.setattr(settings_module, "environ", {})
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


def test_existing_required_settings_constructor_keeps_new_defaults():
    """Old direct constructors gain bounded tuning without supplying new required fields."""
    settings = settings_module.AppSettings(
        database_url="sqlite+aiosqlite://",
        otel_exporter_otlp_endpoint="http://localhost:4317",
        otel_service_name="custom-trace-name",
        log_level="INFO",
        db_pool_size=5,
        db_max_overflow=10,
        deployed_revision="test",
    )
    assert settings.traffic_enabled is True
    assert settings.traffic_interval_seconds == 5
    assert settings.traffic_max_concurrency == 1
    assert settings.traffic_query_limit == 20
    assert settings.traffic_patient_id is None
    assert settings.traffic_seed is None
    assert settings.metric_flush_interval_seconds == 30
    assert settings.db_pool_timeout_seconds == 30
    assert settings.db_statement_timeout_ms == 0
    assert settings.db_observability_enabled is True
    assert settings.db_observability_interval_seconds == 5


def test_environment_overrides_all_workload_and_observation_tuning(monkeypatch):
    """Parse explicit overrides, retaining seed zero and optional disabled switches."""
    monkeypatch.setattr(
        settings_module,
        "environ",
        {
            "TRAFFIC_ENABLED": "false",
            "TRAFFIC_INTERVAL_SECONDS": "0.25",
            "TRAFFIC_MAX_CONCURRENCY": "4",
            "TRAFFIC_QUERY_LIMIT": "37",
            "TRAFFIC_PATIENT_ID": "synthetic-patient",
            "TRAFFIC_SEED": "0",
            "METRIC_FLUSH_INTERVAL_SECONDS": "0.5",
            "DB_POOL_TIMEOUT_SECONDS": "2.5",
            "DB_STATEMENT_TIMEOUT_MS": "250",
            "DB_OBSERVABILITY_ENABLED": "TRUE",
            "DB_OBSERVABILITY_INTERVAL_SECONDS": "1.5",
        },
    )
    settings = settings_module.get_settings()
    assert settings.traffic_enabled is False
    assert settings.traffic_interval_seconds == 0.25
    assert settings.traffic_max_concurrency == 4
    assert settings.traffic_query_limit == 37
    assert settings.traffic_patient_id == "synthetic-patient"
    assert settings.traffic_seed == 0
    assert settings.metric_flush_interval_seconds == 0.5
    assert settings.db_pool_timeout_seconds == 2.5
    assert settings.db_statement_timeout_ms == 250
    assert settings.db_observability_enabled is True
    assert settings.db_observability_interval_seconds == 1.5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("traffic_interval_seconds", 0),
        ("traffic_interval_seconds", float("inf")),
        ("traffic_max_concurrency", 0),
        ("traffic_max_concurrency", float("inf")),
        ("traffic_query_limit", 0),
        ("traffic_query_limit", 1.5),
        ("metric_flush_interval_seconds", float("nan")),
        ("metric_flush_interval_seconds", 0),
        ("db_pool_timeout_seconds", -1),
        ("db_observability_interval_seconds", 0),
        ("db_statement_timeout_ms", -1),
    ],
)
def test_invalid_tuning_cannot_create_unbounded_work_or_busy_observers(field, value):
    """Reject invalid new settings before the lifespan starts any background tasks."""
    with pytest.raises(ValueError, match=field):
        replace(settings_module.get_settings(), **{field: value})


@pytest.mark.parametrize("source", ["constructor", "environment"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("db_pool_size", 0), ("db_pool_size", -1), ("db_max_overflow", -1), ("db_max_overflow", -2)],
)
def test_unbounded_or_negative_pool_capacity_is_rejected(source, field, value, monkeypatch):
    """Reject unlimited pool sentinels and invalid lower values through both settings entry points."""
    with pytest.raises(ValueError, match=field):
        if source == "constructor":
            replace(settings_module.get_settings(), **{field: value})
        else:
            monkeypatch.setattr(settings_module, "environ", {field.upper(): str(value)})
            settings_module.get_settings()


@pytest.mark.parametrize("field", ["db_pool_size", "db_max_overflow"])
@pytest.mark.parametrize("value", [float("inf"), 1.5])
def test_direct_pool_capacity_requires_finite_integer_counts(field, value):
    """Direct dataclass callers cannot bypass finite pool capacity with noninteger counts."""
    with pytest.raises(ValueError, match=field):
        replace(settings_module.get_settings(), **{field: value})


@pytest.mark.parametrize("source", ["constructor", "environment"])
@pytest.mark.parametrize(("pool_size", "max_overflow"), [(1, 0), (5, 10), (10, 20)])
def test_finite_pool_capacity_accepts_boundaries_and_normal_legacy_values(source, pool_size, max_overflow, monkeypatch):
    """A one-connection pool with no overflow and normal positive legacy configurations stay usable."""
    if source == "constructor":
        settings = replace(settings_module.get_settings(), db_pool_size=pool_size, db_max_overflow=max_overflow)
    else:
        monkeypatch.setattr(
            settings_module,
            "environ",
            {"DB_POOL_SIZE": str(pool_size), "DB_MAX_OVERFLOW": str(max_overflow)},
        )
        settings = settings_module.get_settings()
    assert (settings.db_pool_size, settings.db_max_overflow) == (pool_size, max_overflow)


def test_default_pool_capacity_remains_five_plus_ten():
    """Absent pool overrides retain the existing finite legacy defaults."""
    settings = settings_module.get_settings()
    assert (settings.db_pool_size, settings.db_max_overflow) == (5, 10)


def test_container_shares_tuned_metrics_with_stable_alarm_dimension(monkeypatch):
    """Custom trace naming cannot silently detach symptom samples from the query alarm."""
    container = AppContainer()
    container._settings = replace(
        settings_module.get_settings(),
        otel_service_name="custom-trace-name",
        metric_flush_interval_seconds=0.01,
    )
    metrics = container.symptom_metrics
    assert metrics is container.symptom_metrics
    emitted = []
    monkeypatch.setattr(metrics, "_emit", emitted.append)
    monkeypatch.setattr("test_service.services.symptom_metrics.time.monotonic", lambda: metrics._last_flush + 0.02)
    metrics.record_patient_vitals_duration(12)
    assert emitted[0]["ServiceName"] == "healthcare-sensor-app"
    directive = emitted[0]["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "Healthcare/Sensor"
    assert {"Name": "PatientVitalsQueryDuration", "Unit": "Milliseconds"} in directive["Metrics"]
