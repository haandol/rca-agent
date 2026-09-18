import asyncio
import logging
import math
import random
from datetime import UTC, datetime

from test_service.ports.dto.sensor import ReadingType
from test_service.services.sensor import SensorService
from test_service.services.symptom_metrics import SymptomMetrics

logger = logging.getLogger(__name__)

PATIENTS = [f"P-{i:03d}" for i in range(1, 11)]

READING_PROFILES: dict[str, dict] = {
    ReadingType.HEART_RATE: {
        "unit": "bpm",
        "normal": (65.0, 95.0),
        "abnormal_low": (40.0, 58.0),
        "abnormal_high": (105.0, 140.0),
    },
    ReadingType.BLOOD_PRESSURE_SYSTOLIC: {
        "unit": "mmHg",
        "normal": (95.0, 135.0),
        "abnormal_low": (70.0, 88.0),
        "abnormal_high": (145.0, 180.0),
    },
    ReadingType.BLOOD_PRESSURE_DIASTOLIC: {
        "unit": "mmHg",
        "normal": (65.0, 85.0),
        "abnormal_low": (45.0, 58.0),
        "abnormal_high": (95.0, 120.0),
    },
    ReadingType.TEMPERATURE: {
        "unit": "°C",
        "normal": (36.2, 37.8),
        "abnormal_low": (34.5, 35.8),
        "abnormal_high": (38.3, 40.5),
    },
    ReadingType.SPO2: {
        "unit": "%",
        "normal": (96.0, 99.5),
        "abnormal_low": (85.0, 94.0),
        "abnormal_high": (100.0, 100.0),
    },
}

ABNORMAL_RATE = 0.08
_REQUEST_PLAN = ("ingest", "patient_vitals", "alerts") + ("ingest", "patient_vitals") * 5


def _generate_value(reading_type: str, *, rng: random.Random | None = None) -> float:
    """Keep the normal/abnormal mix while allowing an isolated reproducible random stream."""
    source = random if rng is None else rng
    profile = READING_PROFILES[reading_type]
    if source.random() < ABNORMAL_RATE:
        if source.random() < 0.5:
            lo, hi = profile["abnormal_low"]
        else:
            lo, hi = profile["abnormal_high"]
    else:
        lo, hi = profile["normal"]
    return round(source.uniform(lo, hi), 1)


def _generate_batch(*, rng: random.Random | None = None, patient_id: str | None = None) -> list[dict]:
    """Generate bounded batches, optionally pinning patients for repeatable query workloads."""
    source = random if rng is None else rng
    batch_size = source.randint(3, 6)
    readings = []
    for _ in range(batch_size):
        rt = source.choice(list(READING_PROFILES.keys()))
        readings.append(
            {
                "patient_id": patient_id or source.choice(PATIENTS),
                "reading_type": rt,
                "value": _generate_value(rt, rng=rng),
                "unit": READING_PROFILES[rt]["unit"],
                "timestamp": datetime.now(UTC),
            }
        )
    return readings


async def run_traffic_generator(
    sensor_service: SensorService,
    interval: float = 5.0,
    *,
    max_concurrency: int = 1,
    query_limit: int = 20,
    patient_id: str | None = None,
    seed: int | None = None,
    symptom_metrics: SymptomMetrics | None = None,
    reads_only: bool = False,
) -> None:
    """Offer individual requests on fixed slots, independent of completion.

    Ingest and patient-query slots alternate, with one alert slot per six pairs.
    Each slot consumes at most one task; full capacity and missed deadlines are
    counted as skipped without a backlog or catch-up burst. Interval zero remains
    available to legacy callers and yields each turn. Cancellation drains every
    admitted task before returning.
    """
    if not math.isfinite(interval) or interval < 0:
        raise ValueError("interval must be finite and nonnegative")
    if any(not isinstance(value, int) or value < 1 for value in (max_concurrency, query_limit)):
        raise ValueError("max_concurrency and query_limit must be positive integers")
    logger.info("Background traffic generator started (interval=%.1fs, concurrency=%d)", interval, max_concurrency)
    loop = asyncio.get_running_loop()
    next_due = loop.time()
    slot = 0
    active: set[asyncio.Task] = set()
    try:
        while True:
            await asyncio.sleep(max(0.0, next_due - loop.time()))
            for task in tuple(active):
                if task.done():
                    active.remove(task)
                    task.result()

            now = loop.time()
            missed = max(0, math.floor((now - next_due) / interval)) if interval else 0
            slot += missed
            if symptom_metrics is not None:
                symptom_metrics.record_traffic(offered=missed + 1, skipped=missed)
            if len(active) >= max_concurrency:
                if symptom_metrics is not None:
                    symptom_metrics.record_traffic(skipped=1)
            else:
                # Slot-local seeds preserve inputs for matching slots despite skipped work.
                rng = random.Random(f"{seed}:{slot}") if seed is not None else None
                task = asyncio.create_task(
                    _run_request(
                        sensor_service,
                        ("patient_vitals", "alerts")[slot % 2]
                        if reads_only
                        else _REQUEST_PLAN[slot % len(_REQUEST_PLAN)],
                        rng=rng,
                        patient_id=patient_id,
                        query_limit=query_limit,
                        symptom_metrics=symptom_metrics,
                    ),
                    name="healthcare-traffic-request",
                )
                active.add(task)
            slot += 1
            next_due = now + interval
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        logger.info("Traffic generator stopped")


async def _run_request(
    sensor_service: SensorService,
    operation: str,
    *,
    rng: random.Random | None,
    patient_id: str | None,
    query_limit: int,
    symptom_metrics: SymptomMetrics | None,
) -> None:
    """Run one admitted request and account for success, failure or cancellation safely."""
    if symptom_metrics is not None:
        symptom_metrics.record_traffic(started=1)
    failed = cancelled = 0
    try:
        if operation == "ingest":
            batch = (
                _generate_batch()
                if rng is None and patient_id is None
                else _generate_batch(rng=rng, patient_id=patient_id)
            )
            await sensor_service.ingest(batch)
        elif operation == "patient_vitals":
            source = random if rng is None else rng
            await sensor_service.get_patient_vitals(patient_id or source.choice(PATIENTS), limit=query_limit)
        else:
            if patient_id is None:
                await sensor_service.get_alerts(limit=10)
            else:
                await sensor_service.get_alerts(patient_id=patient_id, limit=10)
    except asyncio.CancelledError:
        cancelled = 1
        raise
    except Exception as exc:
        failed = 1
        logger.error("Traffic request failed", extra={"operation": operation, "error_type": type(exc).__name__})
    finally:
        if symptom_metrics is not None:
            symptom_metrics.record_traffic(completed=1, failed=failed, cancelled=cancelled)
