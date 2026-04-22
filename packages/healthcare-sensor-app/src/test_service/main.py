import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from test_service.adapters.secondary.sensor_repository.models import Base
from test_service.di.app_container import AppContainer
from test_service.di.container import Container
from test_service.middleware import FaultFlagMiddleware, LoggingMiddleware
from test_service.services.traffic_generator import run_traffic_generator
from test_service.telemetry import setup_logging, setup_telemetry

container = AppContainer()
containers: list[Container] = [container]


@asynccontextmanager
async def lifespan(_: FastAPI):
    from test_service.adapters.secondary.database_adapter import SqlAlchemyDatabaseAdapter

    db = container.database
    if isinstance(db, SqlAlchemyDatabaseAdapter):
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    traffic_task = asyncio.create_task(
        run_traffic_generator(container.sensor_service, interval=5.0),
    )
    yield
    traffic_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await traffic_task
    for c in containers:
        await c.cleanup()


def create_app() -> FastAPI:
    setup_logging(container.settings)

    app = FastAPI(title="Healthcare Sensor Service", version="0.1.0", lifespan=lifespan)

    app.add_middleware(LoggingMiddleware)
    app.add_middleware(FaultFlagMiddleware, settings=container.settings)

    setup_telemetry(app, container.settings)
    app.include_router(container.create_router())

    return app


app = create_app()
