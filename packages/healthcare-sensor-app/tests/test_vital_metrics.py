"""Unknown/stale observation is not a fresh pending gauge or a successful measurement."""

from test_service.adapters.secondary.vital_repository.postgresql import retry_delay
from test_service.services import symptom_metrics


def test_snapshot_not_reemitted_as_fresh_after_database_read_failure(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(symptom_metrics.time, "time", lambda: clock[0])
    metrics = symptom_metrics.SymptomMetrics("healthcare-sensor-app", flush_interval=1000)
    emitted = []
    monkeypatch.setattr(metrics, "_emit", emitted.append)
    metrics.record_vital_snapshot(
        {
            key: 3
            for key in (
                "pending_count",
                "generated_total",
                "accepted_total",
                "skipped_capacity_total",
                "sql_attempts_total",
                "sql_failures_total",
                "retries_total",
                "committed_total",
            )
        }
    )
    metrics.flush(heartbeat=True)
    assert emitted[-1]["VitalPendingEvents"] == 3
    clock[0] = 180.0
    # No successful snapshot arrives after the simulated DB read failure.
    metrics.flush(heartbeat=True)
    assert "VitalPendingEvents" not in emitted[-1]
    assert "VitalUniqueCommittedTotal" not in emitted[-1]
    assert emitted[-1]["VitalIngestAttempts"] == 0 and emitted[-1]["VitalIngestFailures"] == 0
    assert emitted[-1]["_aws"]["Timestamp"] == 180000


def test_retry_delay_is_positive_exponential_jitter_with_fixed_upper_bound():
    class Low:
        @staticmethod
        def uniform(low, high):
            return low

    class High:
        @staticmethod
        def uniform(low, high):
            return high

    assert [retry_delay(i, rng=Low) for i in range(1, 5)] == [0.5, 1, 2, 4]
    assert [retry_delay(i, rng=High) for i in range(1, 5)] == [1, 2, 4, 8]
    assert retry_delay(1000000, rng=Low) == 30 and retry_delay(1000000, rng=High) == 60


def test_fresh_snapshot_rotates_prior_minute_without_relabeling_old_gauges(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(symptom_metrics.time, "time", lambda: clock[0])
    metrics = symptom_metrics.SymptomMetrics("healthcare-sensor-app", flush_interval=1000)
    emitted = []
    monkeypatch.setattr(metrics, "_emit", emitted.append)
    fields = (
        "pending_count",
        "generated_total",
        "accepted_total",
        "skipped_capacity_total",
        "sql_attempts_total",
        "sql_failures_total",
        "retries_total",
        "committed_total",
    )
    metrics.record_vital_snapshot(dict.fromkeys(fields, 3))
    clock[0] = 180.0
    metrics.record_vital_snapshot(dict.fromkeys(fields, 7))
    assert len(emitted) == 1 and emitted[0]["VitalPendingEvents"] == 3
    assert emitted[0]["_aws"]["Timestamp"] == 100000
    metrics.flush(heartbeat=True)
    assert emitted[-1]["VitalPendingEvents"] == 7 and emitted[-1]["_aws"]["Timestamp"] == 180000
    clock[0] = 240.0
    metrics.flush(heartbeat=True)
    assert "VitalPendingEvents" not in emitted[-1]
    assert "VitalPendingEvents" not in {m["Name"] for m in emitted[-1]["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
