import asyncio
import json
import logging
import math
import threading
import time
from datetime import UTC, datetime

logger = logging.getLogger("healthcare.symptom")

NAMESPACE = "Healthcare/Sensor"

METRIC_INGEST_FAILURES = "VitalIngestFailures"
METRIC_INGEST_ATTEMPTS = "VitalIngestAttempts"
METRIC_ALERT_DELAY_SECONDS = "AbnormalAlertDelaySeconds"
METRIC_INGEST_STARTED = "VitalIngestStarted"
METRIC_INGEST_IN_FLIGHT = "VitalIngestInFlight"
METRIC_PATIENT_VITALS_QUERY_DURATION = "PatientVitalsQueryDuration"
METRIC_TRAFFIC_OFFERED = "TrafficOffered"
METRIC_TRAFFIC_STARTED = "TrafficStarted"
METRIC_TRAFFIC_COMPLETED = "TrafficCompleted"
METRIC_TRAFFIC_SKIPPED = "TrafficSkipped"
METRIC_TRAFFIC_FAILED = "TrafficFailed"
METRIC_TRAFFIC_CANCELLED = "TrafficCancelled"

_FLUSH_INTERVAL_SECONDS = 30.0
_MAX_SAMPLES = 100
_MINUTE_MS = 60_000


class SymptomMetrics:
    """Aggregates domain symptom metrics and emits them as CloudWatch EMF logs.

    The app never calls PutMetricData — the log driver extracts the metrics from
    stdout. That keeps the request path free of metric publishing failures and
    leaves each datapoint next to the log events that produced it.
    """

    def __init__(self, service_name: str, *, flush_interval: float = _FLUSH_INTERVAL_SECONDS) -> None:
        """Keep fixed-size metric buffers; interval zero permits immediate test emission."""
        if not math.isfinite(flush_interval) or flush_interval < 0:
            raise ValueError("flush_interval must be finite and nonnegative")
        self._service_name = service_name
        self._flush_interval = flush_interval
        self._lock = threading.Lock()
        self._attempts = 0
        self._failures = 0
        self._started = 0
        self._in_flight = 0
        self._dirty = False
        self._traffic = dict.fromkeys(
            (
                METRIC_TRAFFIC_OFFERED,
                METRIC_TRAFFIC_STARTED,
                METRIC_TRAFFIC_COMPLETED,
                METRIC_TRAFFIC_SKIPPED,
                METRIC_TRAFFIC_FAILED,
                METRIC_TRAFFIC_CANCELLED,
            ),
            0,
        )
        self._delays: list[float] = []
        self._query_durations: list[float] = []
        self._timestamp_ms = int(time.time() * 1000)
        self._last_flush = time.monotonic()
        logger.info(
            "write_accounting",
            extra={
                "event": "write_accounting",
                "observed_at": datetime.now(UTC).isoformat(),
                "metric_namespace": NAMESPACE,
                "service_name": service_name,
                "attempt_metric": METRIC_INGEST_ATTEMPTS,
                "failure_metric": METRIC_INGEST_FAILURES,
                "attempt_semantics": "completed_successful_rows_plus_failed_rows",
                "failure_semantics": "failed_rows",
                "cancellation_semantics": "excluded_from_completed_counters",
                "success_evidence_event": "write_completed",
                "success_count_field": "count",
                "success_semantics": "committed_rows",
            },
        )

    def record_ingest(self, *, attempted: int, failed: int) -> None:
        """Preserve the legacy completed-reading counters, excluding unfinished work."""
        with self._lock:
            previous = self._rotate_minute_locked()
            self._attempts += attempted
            self._failures += failed
            self._dirty = True
        if previous is not None:
            self._emit(previous)
        self._flush_if_due()

    def record_ingest_started(self, readings: int) -> None:
        """Expose started readings and their live gauge before any repository wait."""
        with self._lock:
            previous = self._rotate_minute_locked()
            self._started += readings
            self._in_flight += readings
            self._dirty = True
        if previous is not None:
            self._emit(previous)
        self._flush_if_due()

    def record_ingest_finished(self, readings: int, *, failed: bool = False, cancelled: bool = False) -> None:
        """Release in-flight readings on every exit; cancellation is not a legacy completion."""
        with self._lock:
            previous = self._rotate_minute_locked()
            self._in_flight -= readings
            if not cancelled:
                self._attempts += readings
                self._failures += readings if failed else 0
            self._dirty = True
        if previous is not None:
            self._emit(previous)
        self._flush_if_due()

    def record_traffic(
        self,
        *,
        offered: int = 0,
        started: int = 0,
        completed: int = 0,
        skipped: int = 0,
        failed: int = 0,
        cancelled: int = 0,
    ) -> None:
        """Count request slots and terminal outcomes without storing per-request data."""
        with self._lock:
            previous = self._rotate_minute_locked()
            for name, value in (
                (METRIC_TRAFFIC_OFFERED, offered),
                (METRIC_TRAFFIC_STARTED, started),
                (METRIC_TRAFFIC_COMPLETED, completed),
                (METRIC_TRAFFIC_SKIPPED, skipped),
                (METRIC_TRAFFIC_FAILED, failed),
                (METRIC_TRAFFIC_CANCELLED, cancelled),
            ):
                self._traffic[name] += value
            self._dirty = True
        if previous is not None:
            self._emit(previous)
        self._flush_if_due()

    def record_alert_delay(self, seconds: float) -> None:
        """Retain abnormal-ingest delay in seconds with a bounded EMF sample buffer."""
        self._record_sample(self._delays, seconds)

    def record_patient_vitals_duration(self, milliseconds: float) -> None:
        """Record service-level query latency, including failed and cancelled calls."""
        self._record_sample(self._query_durations, milliseconds)

    def _record_sample(self, samples: list[float], value: float) -> None:
        """Emit at capacity so no sample array exceeds EMF's 100-value limit."""
        if not math.isfinite(value) or value < 0:
            raise ValueError("metric samples must be finite and nonnegative")
        with self._lock:
            previous = self._rotate_minute_locked()
            samples.append(value)
            self._dirty = True
            payload = self._drain_locked() if len(samples) == _MAX_SAMPLES else None
        if previous is not None:
            self._emit(previous)
        if payload is not None:
            self._emit(payload)
        self._flush_if_due()

    def _flush_if_due(self) -> None:
        """Keep immediate/manual recording compatibility alongside the periodic publisher."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_flush < self._flush_interval:
                return
            previous = self._rotate_minute_locked()
            payload = self._drain_locked()
            self._last_flush = now
        if previous is not None:
            self._emit(previous)
        if payload is not None:
            self._emit(payload)

    def flush(self, *, heartbeat: bool = False) -> None:
        """Drain counters and samples, preserving live gauges across periodic heartbeats."""
        with self._lock:
            previous = self._rotate_minute_locked()
            payload = self._drain_locked(heartbeat=heartbeat)
            self._last_flush = time.monotonic()
        if previous is not None:
            self._emit(previous)
        if payload is not None:
            self._emit(payload)

    async def run_periodic_flush(self, stop_event: asyncio.Event, *, interval: float | None = None) -> None:
        """Publish even while requests stall and perform a final flush on stop or cancellation."""
        interval = self._flush_interval if interval is None else interval
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("periodic flush interval must be finite and positive")
        try:
            while not stop_event.is_set():
                try:
                    async with asyncio.timeout(interval):
                        await stop_event.wait()
                except TimeoutError:
                    self.flush(heartbeat=True)
        finally:
            self.flush(heartbeat=True)

    def _rotate_minute_locked(self) -> dict | None:
        """Separate source minutes before any counter, sample or live-gauge update.

        Keep only one bounded bucket. Its timestamp is the first record's time,
        not the later publishing time. Boundary/capacity drains do not move the
        monotonic flush deadline. A clean heartbeat observes the current time.
        """
        now_ms = int(time.time() * 1000)
        previous = None
        if self._dirty and now_ms // _MINUTE_MS != self._timestamp_ms // _MINUTE_MS:
            previous = self._drain_locked()
        if not self._dirty:
            self._timestamp_ms = now_ms
        return previous

    def _drain_locked(self, *, heartbeat: bool = False) -> dict | None:
        """Build one EMF document and reset interval totals without resetting in-flight work."""
        if not self._dirty and not self._in_flight and not heartbeat:
            return None

        metrics: list[dict] = [
            {"Name": METRIC_INGEST_ATTEMPTS, "Unit": "Count"},
            {"Name": METRIC_INGEST_FAILURES, "Unit": "Count"},
            {"Name": METRIC_INGEST_STARTED, "Unit": "Count"},
            {"Name": METRIC_INGEST_IN_FLIGHT, "Unit": "Count"},
            *({"Name": name, "Unit": "Count"} for name in self._traffic),
        ]
        values: dict[str, float | list[float]] = {
            METRIC_INGEST_ATTEMPTS: self._attempts,
            METRIC_INGEST_FAILURES: self._failures,
            METRIC_INGEST_STARTED: self._started,
            METRIC_INGEST_IN_FLIGHT: self._in_flight,
            **self._traffic,
        }
        if self._delays:
            metrics.append({"Name": METRIC_ALERT_DELAY_SECONDS, "Unit": "Seconds"})
            values[METRIC_ALERT_DELAY_SECONDS] = list(self._delays)
        if self._query_durations:
            metrics.append({"Name": METRIC_PATIENT_VITALS_QUERY_DURATION, "Unit": "Milliseconds"})
            values[METRIC_PATIENT_VITALS_QUERY_DURATION] = list(self._query_durations)

        self._attempts = 0
        self._failures = 0
        self._started = 0
        self._dirty = False
        self._traffic = dict.fromkeys(self._traffic, 0)
        self._delays.clear()
        self._query_durations.clear()

        return {
            "_aws": {
                "Timestamp": self._timestamp_ms,
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
