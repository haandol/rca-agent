from __future__ import annotations

from fastapi import APIRouter

from test_service.config import AppSettings, get_settings
from test_service.di.container import Container
from test_service.ports.interfaces.database import DatabasePort
from test_service.ports.interfaces.sensor_reading_repository import SensorReadingRepositoryPort
from test_service.services.fault import FaultInjectionService
from test_service.services.health import HealthService
from test_service.services.sensor import SensorService


class AppContainer(Container):
    def __init__(self) -> None:
        self._settings: AppSettings | None = None
        self._database: DatabasePort | None = None
        self._sensor_repository: SensorReadingRepositoryPort | None = None
        self._sensor_service: SensorService | None = None
        self._health_service: HealthService | None = None
        self._fault_service: FaultInjectionService | None = None

    @property
    def settings(self) -> AppSettings:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    @property
    def database(self) -> DatabasePort:
        if self._database is None:
            from test_service.adapters.secondary.database_adapter import SqlAlchemyDatabaseAdapter

            self._database = SqlAlchemyDatabaseAdapter(self.settings)
        return self._database

    @property
    def sensor_repository(self) -> SensorReadingRepositoryPort:
        if self._sensor_repository is None:
            from test_service.adapters.secondary.sensor_repository.sqlalchemy_sensor_repository import (
                SqlAlchemySensorReadingRepository,
            )

            self._sensor_repository = SqlAlchemySensorReadingRepository(self.database)
        return self._sensor_repository

    @property
    def sensor_service(self) -> SensorService:
        if self._sensor_service is None:
            self._sensor_service = SensorService(self.sensor_repository)
        return self._sensor_service

    @property
    def health_service(self) -> HealthService:
        if self._health_service is None:
            self._health_service = HealthService(self.database)
        return self._health_service

    @property
    def fault_service(self) -> FaultInjectionService:
        if self._fault_service is None:
            self._fault_service = FaultInjectionService(self.database)
        return self._fault_service

    def create_router(self) -> APIRouter:
        from test_service.adapters.primary.alerts.alert_controller import AlertController
        from test_service.adapters.primary.fault.fault_controller import FaultController
        from test_service.adapters.primary.health.health_controller import HealthController
        from test_service.adapters.primary.patients.patient_controller import PatientController
        from test_service.adapters.primary.sensors.sensor_controller import SensorController

        router = APIRouter()
        router.include_router(HealthController(self.health_service).router)
        router.include_router(SensorController(self.sensor_service).router)
        router.include_router(PatientController(self.sensor_service).router)
        router.include_router(AlertController(self.sensor_service).router)
        router.include_router(FaultController(self.fault_service, self.settings).router)
        return router

    async def cleanup(self) -> None:
        if self._database is not None:
            await self._database.dispose()
