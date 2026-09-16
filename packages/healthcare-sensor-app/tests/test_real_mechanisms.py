"""Generic SQL privacy, cancellation and fixture isolation contracts."""

import asyncio
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_service.ports.interfaces.database import DatabasePort
from test_service.revision.session import session_scope
from test_service.services.db_observability import current_operation, install_hooks, operation_context

PACKAGE = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_sql_observation_never_records_parameters_or_inline_literals(db_engine, caplog):
    install_hooks(db_engine)
    async with db_engine.connect() as conn:
        with caplog.at_level(logging.INFO):
            with operation_context("outer"):
                outer = current_operation()
                with operation_context("inner"):
                    assert current_operation() is outer
                    await conn.execute(text("SELECT :secret"), {"secret": "sensitive-bound-value"})
                    await conn.execute(text("SELECT 'sensitive-inline-value'"))
                assert outer.sql_count == 2
                assert outer.sql_time_ms > 0
    assert current_operation() is None
    serialized = json.dumps([record.__dict__ for record in caplog.records], default=str)
    assert "sensitive-bound-value" not in serialized
    assert "sensitive-inline-value" not in serialized
    assert len([r for r in caplog.records if r.getMessage() == "db_operation"]) == 1


async def test_normal_scope_returns_transaction_on_cancellation(db_engine):
    owned = set()
    factory = async_sessionmaker(db_engine)
    ready = asyncio.Event()

    async def consumer():
        async with session_scope(factory, owned) as session:
            await session.execute(text("SELECT 1"))
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(consumer())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not owned
    async with factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


def test_local_runner_refuses_defaults_and_unowned_database(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE / "demo"))
    runner = load_file("proof_runner", PACKAGE / "demo/local_runner.py")
    env_file = tmp_path / "database.env"
    for dsn in (
        "postgresql+asyncpg://u:p@localhost:5432/rca_demo",
        "postgresql+asyncpg://u:p@127.0.0.1:5432/rca_demo",
        "postgresql+asyncpg://u:p@127.0.0.1:32768/another_project",
        "postgresql+asyncpg://u:p@external.example:32768/rca_demo",
    ):
        env_file.write_text("DATABASE_URL=" + dsn)
        with pytest.raises(ValueError, match="caller-owned"):
            runner.read_dsn(env_file, 32768)
    env_file.write_text("DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:32768/rca_demo")
    assert runner.read_dsn(env_file, 32768)


async def test_context_bridge_forwards_consumer_exception_and_closes_generator():
    received = []

    async def legacy_generator():
        try:
            yield "session"
        except ValueError:
            received.append("rollback")
            raise
        finally:
            received.append("close")

    port = SimpleNamespace(session=legacy_generator)
    with pytest.raises(ValueError, match="consumer"):
        async with DatabasePort.session_context(port):
            raise ValueError("consumer")
    assert received == ["rollback", "close"]
