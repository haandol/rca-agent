"""Internal read-only PostgreSQL proof transport; credentials never enter output."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError


def database_url(path, expected):
    """Require an explicit credential file and the normal deployment's exact database coordinates."""
    from sqlalchemy.engine import make_url

    entries = {}
    for line in Path(path).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            entries[key.strip()] = value.strip().strip("'\"")
    url = make_url(entries.get("DATABASE_URL", ""))
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != expected["host"]
        or (url.port or 5432) != expected["port"]
        or url.database != expected["database"]
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expected["schema"])
    ):
        raise ValueError("database coordinates differ from the normal deployment")
    return url


async def read_proof(request):
    """Call the actual repository under read-only sessions; never initialize schema or process events."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    from test_service.adapters.secondary.vital_repository.postgresql import (
        PostgreSQLVitalRepository,
    )

    target = request["target"]
    engine = create_async_engine(
        database_url(request["env_file"], target),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "default_transaction_read_only": "on",
                "search_path": target["schema"],
                "application_name": "rca-backlog-readonly-proof",
            }
        },
    )
    try:
        async with engine.connect() as conn:
            identity = (
                (
                    await conn.execute(
                        text(
                            "SELECT current_database() AS database, current_schema() AS schema, "
                            "current_setting('transaction_read_only') AS read_only"
                        )
                    )
                )
                .mappings()
                .one()
            )
            if (
                identity["database"] != target["database"]
                or identity["schema"] != target["schema"]
                or identity["read_only"] != "on"
            ):
                raise ValueError("database identity/read-only session mismatch")
        repo = PostgreSQLVitalRepository(engine)
        if request["operation"] == "checkpoint":
            result = await repo.checkpoint()
        elif request["operation"] == "cohort":
            cohort = request["cohort"]
            result = await repo.cohort(**cohort)
        else:
            raise ValueError("unsupported read-only proof operation")
        module = sys.modules[PostgreSQLVitalRepository.__module__]
        return {
            "nonce": request["nonce"],
            "target": target,
            "transport": "direct_postgresql_readonly",
            "repository_sha256": hashlib.sha256(
                Path(module.__file__).read_bytes()
            ).hexdigest(),
            "result": result,
        }
    finally:
        await engine.dispose()


def main():
    """Accept only the caller's internal request and hide provider/credential exception text."""
    try:
        request = json.load(sys.stdin)
        sys.path.insert(
            0,
            str(
                Path(__file__).resolve().parents[1]
                / "packages/healthcare-sensor-app/src"
            ),
        )
        print(json.dumps(asyncio.run(read_proof(request)), sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ImportError,
        RuntimeError,
        SQLAlchemyError,
    ) as error:
        print(json.dumps({"error_type": type(error).__name__, "complete": False}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
