"""Fixed, one-shot read-only backlog probe; never starts FastAPI, production, or migration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from test_service.adapters.secondary.vital_repository.postgresql import PostgreSQLVitalRepository
from test_service.revision.manifest import source_manifest


def connection_url():
    """Use only the pinned task's existing DB environment and injected secrets; no CLI credential overrides."""
    if value := os.environ.get("DATABASE_URL"):
        url = make_url(value)
        if url.drivername != "postgresql+asyncpg":
            raise ValueError("unsupported database driver")
        return url
    return URL.create(
        "postgresql+asyncpg",
        username=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        database=os.environ["DB_NAME"],
    )


async def read_once(url, schema, operation, cohort=None):
    """Read actual retained identity/inbox/measurement rows with read-only transactions and close the connection."""
    if not isinstance(schema, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid schema")
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "default_transaction_read_only": "on",
                "search_path": schema,
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
            if identity["schema"] != schema or identity["read_only"] != "on":
                raise ValueError("database identity/read-only session mismatch")
        repository = PostgreSQLVitalRepository(engine)
        if operation == "checkpoint":
            result = await repository.checkpoint()
        elif operation == "cohort" and isinstance(cohort, dict):
            result = await repository.cohort(**cohort)
        else:
            raise ValueError("unsupported proof operation")
        return {"database": identity["database"], "schema": schema, "result": result}
    finally:
        await engine.dispose()


def main(argv=None):
    """Emit one nonce-bound summary from a verified normal image; provider/private error prose never escapes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("checkpoint", "cohort"), required=True)
    parser.add_argument("--probe-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--epoch")
    parser.add_argument("--lower-exclusive", type=int)
    parser.add_argument("--upper-inclusive", type=int)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-f0-9]{32}", args.probe_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", args.run_id):
            raise ValueError("invalid probe identity")
        manifest = source_manifest()
        if manifest.get("verified") is not True or manifest.get("revision") != "v1":
            raise ValueError("probe requires the verified normal build")
        cohort = None
        if args.operation == "cohort":
            if (
                not isinstance(args.epoch, str)
                or not re.fullmatch(r"[a-f0-9-]{36}", args.epoch)
                or args.lower_exclusive is None
                or args.upper_inclusive is None
                or not 0 <= args.lower_exclusive <= args.upper_inclusive
            ):
                raise ValueError("invalid fixed cohort")
            cohort = {
                "epoch": args.epoch,
                "lower_exclusive": args.lower_exclusive,
                "upper_inclusive": args.upper_inclusive,
            }
        elif any(value is not None for value in (args.epoch, args.lower_exclusive, args.upper_inclusive)):
            raise ValueError("checkpoint does not accept range overrides")
        result = asyncio.run(read_once(connection_url(), args.schema, args.operation, cohort))
        print(
            json.dumps(
                {
                    "event": "vital_backlog_proof",
                    "probe_id": args.probe_id,
                    "run_id": args.run_id,
                    "operation": args.operation,
                    "source_fingerprint": manifest["fingerprint"],
                    "probe_source_sha256": manifest["files"]["backlog_probe.py"],
                    "observed_at": datetime.now(UTC).isoformat(),
                    **result,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, SQLAlchemyError) as error:
        print(
            json.dumps(
                {
                    "event": "vital_backlog_proof_error",
                    "probe_id": args.probe_id,
                    "run_id": args.run_id,
                    "error_type": type(error).__name__,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
