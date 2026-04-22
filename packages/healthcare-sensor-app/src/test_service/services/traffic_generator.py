import asyncio
import logging
import random
from datetime import UTC, datetime

from test_service.ports.dto.sensor import ReadingType
from test_service.services.sensor import SensorService

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


def _generate_value(reading_type: str) -> float:
    profile = READING_PROFILES[reading_type]
    if random.random() < ABNORMAL_RATE:
        if random.random() < 0.5:
            lo, hi = profile["abnormal_low"]
        else:
            lo, hi = profile["abnormal_high"]
    else:
        lo, hi = profile["normal"]
    return round(random.uniform(lo, hi), 1)


def _generate_batch() -> list[dict]:
    batch_size = random.randint(3, 6)
    readings = []
    for _ in range(batch_size):
        rt = random.choice(list(READING_PROFILES.keys()))
        readings.append(
            {
                "patient_id": random.choice(PATIENTS),
                "reading_type": rt,
                "value": _generate_value(rt),
                "unit": READING_PROFILES[rt]["unit"],
                "timestamp": datetime.now(UTC),
            }
        )
    return readings


async def run_traffic_generator(
    sensor_service: SensorService,
    interval: float = 5.0,
) -> None:
    logger.info("Background traffic generator started (interval=%.1fs)", interval)
    cycle = 0
    while True:
        try:
            batch = _generate_batch()
            await sensor_service.ingest(batch)

            if cycle % 6 == 0:
                patient = random.choice(PATIENTS)
                await sensor_service.get_patient_vitals(patient, limit=20)
                await sensor_service.get_alerts(limit=10)

            cycle += 1
        except asyncio.CancelledError:
            logger.info("Traffic generator stopped")
            raise
        except Exception:
            logger.exception("Traffic generator error")

        await asyncio.sleep(interval)
