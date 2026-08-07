import asyncio

import pytest

from test_service.services import traffic_generator


class RecordingSensorService:
    def __init__(self, cycles: int) -> None:
        self._cycles = cycles
        self.ingest_calls = 0
        self.patient_vitals_calls = 0
        self.alert_calls = 0

    async def ingest(self, _batch: list[dict]) -> None:
        self.ingest_calls += 1

    async def get_patient_vitals(self, _patient: str, *, limit: int) -> None:
        assert limit == 20
        self.patient_vitals_calls += 1
        if self.patient_vitals_calls == self._cycles:
            raise asyncio.CancelledError

    async def get_alerts(self, *, limit: int) -> None:
        assert limit == 10
        self.alert_calls += 1


@pytest.mark.asyncio
async def test_background_traffic_reads_patient_vitals_every_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_capacity = 15
    service = RecordingSensorService(cycles=pool_capacity)
    monkeypatch.setattr(traffic_generator, "_generate_batch", lambda: [])

    with pytest.raises(asyncio.CancelledError):
        await traffic_generator.run_traffic_generator(service, interval=0)

    assert service.ingest_calls == pool_capacity
    assert service.patient_vitals_calls == pool_capacity
    assert service.alert_calls == 3
