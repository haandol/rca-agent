"""Durable admission and one bounded storage attempt; waiting belongs outside the repository."""

from abc import ABC, abstractmethod

from test_service.ports.dto.vital import Admission, AttemptResult, VitalEvent


class VitalRepositoryPort(ABC):
    @abstractmethod
    async def admit(self, event: VitalEvent) -> Admission: ...

    @abstractmethod
    async def generate_once(self) -> Admission: ...

    @abstractmethod
    async def process_one(self, slot: int) -> AttemptResult: ...

    @abstractmethod
    async def snapshot(self) -> dict: ...

    @abstractmethod
    async def checkpoint(self) -> dict: ...

    @abstractmethod
    async def cohort(self, epoch: str, lower_exclusive: int, upper_inclusive: int) -> dict: ...
