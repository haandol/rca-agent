import json
import logging
import threading
import time

logger = logging.getLogger("healthcare.symptom")

NAMESPACE = "Healthcare/Sensor"

METRIC_INGEST_FAILURES = "VitalIngestFailures"
METRIC_INGEST_ATTEMPTS = "VitalIngestAttempts"
METRIC_ALERT_DELAY_SECONDS = "AbnormalAlertDelaySeconds"

_FLUSH_INTERVAL_SECONDS = 30.0


class SymptomMetrics:
    """Aggregates domain symptom metrics and emits them as CloudWatch EMF logs.

    The app never calls PutMetricData — the log driver extracts the metrics from
    stdout. That keeps the request path free of metric publishing failures and
    leaves each datapoint next to the log events that produced it.
    """

    def __init__(self, service_name: str, *, flush_interval: float = _FLUSH_INTERVAL_SECONDS) -> None:
        self._service_name = service_name
        self._flush_interval = flush_interval
        self._lock = threading.Lock()
        self._attempts = 0
        self._failures = 0
        self._delays: list[float] = []
        self._last_flush = time.monotonic()

    def record_ingest(self, *, attempted: int, failed: int) -> None:
        with self._lock:
            self._attempts += attempted
            self._failures += failed
        self._flush_if_due()

    def record_alert_delay(self, seconds: float) -> None:
        with self._lock:
            self._delays.append(seconds)
        self._flush_if_due()

    def _flush_if_due(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_flush < self._flush_interval:
                return
            payload = self._drain_locked()
            self._last_flush = now
        if payload is not None:
            self._emit(payload)

    def flush(self) -> None:
        with self._lock:
            payload = self._drain_locked()
            self._last_flush = time.monotonic()
        if payload is not None:
            self._emit(payload)

    def _drain_locked(self) -> dict | None:
        if not self._attempts and not self._failures and not self._delays:
            return None

        metrics: list[dict] = [
            {"Name": METRIC_INGEST_ATTEMPTS, "Unit": "Count"},
            {"Name": METRIC_INGEST_FAILURES, "Unit": "Count"},
        ]
        values: dict[str, float | list[float]] = {
            METRIC_INGEST_ATTEMPTS: self._attempts,
            METRIC_INGEST_FAILURES: self._failures,
        }
        if self._delays:
            metrics.append({"Name": METRIC_ALERT_DELAY_SECONDS, "Unit": "Seconds"})
            values[METRIC_ALERT_DELAY_SECONDS] = list(self._delays)

        self._attempts = 0
        self._failures = 0
        self._delays.clear()

        return {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": NAMESPACE,
                        "Dimensions": [["ServiceName"]],
                        "Metrics": metrics,
                    }
                ],
            },
            "ServiceName": self._service_name,
            **values,
        }

    @staticmethod
    def _emit(payload: dict) -> None:
        # EMF requires the metric document to be the whole log line, so this
        # bypasses the JSON formatter used by the rest of the app's logging.
        print(json.dumps(payload), flush=True)  # noqa: T201
