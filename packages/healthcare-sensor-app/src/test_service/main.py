import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from test_service.adapters.secondary.sensor_repository.models import Base
from test_service.di.app_container import AppContainer
from test_service.di.container import Container
from test_service.middleware import FaultFlagMiddleware, LoggingMiddleware
from test_service.services.runtime_identity import runtime_identity
from test_service.services.traffic_generator import run_traffic_generator
from test_service.telemetry import setup_logging, setup_telemetry

container = AppContainer()
containers: list[Container] = [container]


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Own bounded workload and observers, draining them before database disposal.

    Metric publication runs independently of request completion. Shutdown first
    drains requests and database observation, then flushes final counters and
    attempts every container cleanup even if a background task failed.
    """
    from test_service.adapters.secondary.database_adapter import SqlAlchemyDatabaseAdapter

    settings = container.settings
    metrics = container.symptom_metrics
    observer_stop = asyncio.Event()
    metrics_stop = asyncio.Event()
    workers: list[asyncio.Task] = []
    flush_task: asyncio.Task | None = None
    try:
        logging.getLogger(__name__).info("ecs_runtime_identity", extra=await runtime_identity())
        db = container.database
        if isinstance(db, SqlAlchemyDatabaseAdapter):
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        flush_task = asyncio.create_task(
            metrics.run_periodic_flush(metrics_stop, interval=settings.metric_flush_interval_seconds),
            name="healthcare-metric-flush",
        )
        if settings.db_observability_enabled and isinstance(db, SqlAlchemyDatabaseAdapter):
            workers.append(
                asyncio.create_task(
                    db.observe(observer_stop, interval=settings.db_observability_interval_seconds),
                    name="healthcare-db-observer",
                )
            )
        if settings.traffic_enabled:
            workers.append(
                asyncio.create_task(
                    run_traffic_generator(
                        container.sensor_service,
                        interval=settings.traffic_interval_seconds,
                        max_concurrency=settings.traffic_max_concurrency,
                        query_limit=settings.traffic_query_limit,
                        patient_id=settings.traffic_patient_id,
                        seed=settings.traffic_seed,
                        symptom_metrics=metrics,
                    ),
                    name="healthcare-traffic-scheduler",
                )
            )
        yield
    finally:
        observer_stop.set()
        errors = await _cancel_and_drain(workers)
        metrics_stop.set()
        if flush_task is not None:
            errors.extend(await _cancel_and_drain([flush_task]))
        try:
            # Also covers shutdown before the periodic coroutine gets its first turn.
            metrics.flush()
        except Exception as exc:
            errors.append(exc)
        for c in containers:
            try:
                await c.cleanup()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("Healthcare background cleanup failed", errors)


async def _cancel_and_drain(tasks: list[asyncio.Task]) -> list[BaseException]:
    """Await every owned task after cancellation so failures cannot skip sibling cleanup."""
    for task in tasks:
        if not task.done():
            task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    return [
        outcome
        for outcome in outcomes
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError)
    ]


def create_app() -> FastAPI:
    """Place the safe HTTP boundary around application middleware, inside server tracing."""
    setup_logging(container.settings)

    app = FastAPI(title="Healthcare Sensor Service", version="0.1.0", lifespan=lifespan)

    app.add_middleware(FaultFlagMiddleware, settings=container.settings)
    app.add_middleware(LoggingMiddleware)

    setup_telemetry(app, container.settings)
    app.include_router(container.create_router())

    return app


app = create_app()
