"""Durable generation and independent bounded consumers; waiting never owns a database connection."""

import asyncio
import logging

from test_service.config.settings import VITAL_WORKER_SLOTS
from test_service.ports.dto.vital import VitalEvent
from test_service.ports.interfaces.vital_repository import VitalRepositoryPort

logger = logging.getLogger(__name__)


class VitalService:
    def __init__(self, repository: VitalRepositoryPort, metrics):
        """Keep storage authority in the repository and scheduling lifetime in this service."""
        self.repository = repository
        self.metrics = metrics

    async def admit(self, event: VitalEvent):
        """Expose the committed admission result without implying that a queued measurement is saved."""
        return await self.repository.admit(event)

    async def _producer(self):
        """Offer current slots independently from worker progress; database coordination chooses the winner."""
        while True:
            try:
                await self.repository.generate_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("vital_generation_unconfirmed", extra={"error_type": type(exc).__name__})
            await asyncio.sleep(0.2)

    async def _consumer(self, slot: int):
        """Retry only due durable records; errors never erase queued input or cause a tight loop."""
        while True:
            delay = 0.0
            try:
                result = await self.repository.process_one(slot)
                if result.state in {"NO_WORK", "BUSY"}:
                    delay = 0.2
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("vital_attempt_unconfirmed", extra={"error_type": type(exc).__name__})
                delay = 1.0
            await asyncio.sleep(delay)

    async def _snapshot(self):
        """Publish independently sampled database totals without adding each worker's copy together."""
        while True:
            try:
                self.metrics.record_vital_snapshot(await self.repository.snapshot())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("vital_snapshot_unavailable", extra={"error_type": type(exc).__name__})
            await asyncio.sleep(5)

    async def run(self, *, generate: bool):
        """Cancel and drain every owned coroutine on shutdown; admitted backlog stays in PostgreSQL."""
        async with asyncio.TaskGroup() as group:
            if generate:
                group.create_task(self._producer(), name="vital-producer")
            for slot in range(VITAL_WORKER_SLOTS):
                group.create_task(self._consumer(slot), name=f"vital-consumer-{slot}")
            group.create_task(self._snapshot(), name="vital-snapshot")

    async def checkpoint(self):
        """Read cohort bounds without creating or completing events."""
        return await self.repository.checkpoint()

    async def cohort(self, epoch: str, lower_exclusive: int, upper_inclusive: int):
        """Verify an explicitly fixed interval, returning only counts and integrity status."""
        return await self.repository.cohort(epoch, lower_exclusive, upper_inclusive)
