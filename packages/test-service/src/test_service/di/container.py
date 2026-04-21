from __future__ import annotations

from abc import ABC, abstractmethod

from fastapi import APIRouter

from test_service.config import AppSettings


class Container(ABC):
    @property
    @abstractmethod
    def settings(self) -> AppSettings: ...

    @abstractmethod
    def create_router(self) -> APIRouter: ...

    @abstractmethod
    async def cleanup(self) -> None: ...
