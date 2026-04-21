from contextlib import asynccontextmanager

from fastapi import FastAPI

from test_service.adapters.secondary.sensor_repository.models import Base
from test_service.di.app_container import AppContainer
from test_service.di.container import Container
from test_service.middleware import LoggingMiddleware
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
    yield
    for c in containers:
        await c.cleanup()


def create_app() -> FastAPI:
    setup_logging(container.settings)

    app = FastAPI(title="Healthcare Sensor Service", version="0.1.0", lifespan=lifespan)

    app.add_middleware(LoggingMiddleware)

    setup_telemetry(app, container.settings)
    app.include_router(container.create_router())

    return app


app = create_app()
