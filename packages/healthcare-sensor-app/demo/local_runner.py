"""Normal -> fault -> restore proofs using only an explicit local PostgreSQL DSN.

Creates one unique schema, compiles independent source trees, launches service
phases, writes JSON evidence, then drops only that schema in finally. Never starts
or stops Docker, changes database roles, or falls back to default app settings.
"""

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
from build_revision import capture_source_snapshot, compile_revision
from sqlalchemy.engine import make_url

PACKAGE = Path(__file__).resolve().parents[1]
PROCESS_TERM_TIMEOUT_SECONDS = 15.0
PROCESS_KILL_TIMEOUT_SECONDS = 5.0


async def wait_for_phase_exit(process: asyncio.subprocess.Process) -> None:
    """Observe leader exit independently of pipes inherited by descendants.

    communicate() drains output concurrently, but its EOF (and sometimes wait())
    can be delayed by a descendant after the phase leader crashes.
    """
    while process.returncode is None:
        await asyncio.sleep(0.02)


async def wait_for_group_exit(pgid: int) -> None:
    """Wait for the entire owned group to disappear, including orphaned members."""
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # A dying group may briefly reject the zero-signal probe. Absence
            # is still unproven: keep waiting within the caller's time bound.
            pass
        await asyncio.sleep(0.02)


async def reap_phase_group(process: asyncio.subprocess.Process, output_task: asyncio.Task) -> None:
    """Stop only the group created for this phase, even if its leader has exited.

    A phase is spawned with start_new_session=True, making its PID the owned
    process-group ID. TERM permits transaction rollback; a bounded KILL fallback
    handles an unresponsive descendant. Never infer group death from returncode.
    """
    pgid = process.pid
    if pgid <= 1 or pgid == os.getpgrp():
        raise ValueError("Refusing to signal a group not isolated from the runner")
    try:
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(wait_for_group_exit(pgid), PROCESS_TERM_TIMEOUT_SECONDS)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            await asyncio.wait_for(wait_for_group_exit(pgid), PROCESS_KILL_TIMEOUT_SECONDS)
        if not output_task.cancelled():
            await asyncio.wait_for(asyncio.shield(output_task), PROCESS_KILL_TIMEOUT_SECONDS)
    finally:
        if not output_task.done():
            output_task.cancel()
            with suppress(asyncio.CancelledError):
                await output_task


def read_dsn(path: Path, expected_port: int) -> str:
    """Reject defaults and any database outside the caller-owned local fixture."""
    entries = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            entries[key.strip()] = value.strip().strip("'\"")
    dsn = entries.get("DATABASE_URL")
    if not dsn:
        raise ValueError("DATABASE_URL missing from explicit env file")
    url = make_url(dsn)
    if (
        url.host != "127.0.0.1"
        or url.port != expected_port
        or url.port == 5432
        or url.database != "rca_demo"
        or url.drivername != "postgresql+asyncpg"
    ):
        raise ValueError("DSN does not match the caller-owned local PostgreSQL fixture")
    return dsn


def check_proof(phases):
    """Require actual native errors, stable schema and successful post-commit restoration."""
    assert [p["phase"] for p in phases] == ["normal", "fault", "restore"]
    assert len({p["source"]["base_fingerprint"] for p in phases}) == 1
    assert len({tuple(p["schema_snapshot"]["column_names"]) for p in phases}) == 1
    for phase in phases:
        bad = phase["phase"] == "fault"
        assert phase["write_statuses"] == [500 if bad else 200] * 3
        assert phase["rows_after"] - phase["rows_before"] == (0 if bad else 6)
        assert phase["read_status"] == phase["alerts_status"] == 200
        assert phase["read_count"] == phase["alert_count"] == phase["rows_after"]
        assert phase["existing_rows_preserved"]
        assert phase["health"]["status"] == "ok" and phase["health"]["db_connected"] is True
        assert phase["checked_out"] == phase["owned_sessions_after_dispose"] == 0
        assert phase["canaries_absent"]
        errors = [e for e in phase["events"] if e.get("event") == "db_write_error"]
        completed = [e for e in phase["events"] if e.get("event") == "write_completed"]
        assert len(errors) == (3 if bad else 0)
        assert phase["source"]["verified"]
        assert any(e.get("event") == "db_schema_snapshot" for e in phase["events"])
        assert any(e.get("event") == "write_accounting" for e in phase["events"])
        for error in errors:
            assert any(
                e.get("event") == "db_sql"
                and e.get("outcome") == "error"
                and e.get("request_id") == error["request_id"]
                and e.get("sql_hash") == error["sql_hash"]
                for e in phase["events"]
            )

        assert all(e["sqlstate"] == "42703" and e["error_type"] == "UndefinedColumnError" for e in errors)
        assert len(completed) == (0 if bad else 3)
        assert all(e["count"] == 2 and e["completion_semantics"] == "committed_rows" for e in completed)
        assert phase["metrics"]["VitalIngestAttempts"] == 6
        assert phase["metrics"]["VitalIngestFailures"] == (6 if bad else 0)
        assert phase["metrics"]["VitalIngestInFlight"] == 0
    return {
        "single_insert_column": True,
        "native_42703": True,
        "no_partial_writes": True,
        "read_health_preserved": True,
        "no_connection_leaks": True,
        "canaries_absent": True,
    }


async def run(args):
    """Execute frozen revisions against the owned fixture and preserve evidence through cleanup."""
    args.output = args.output.resolve()
    started_at = datetime.now(UTC).isoformat()
    dsn = read_dsn(args.env_file, args.expected_port)
    native = make_url(dsn).set(drivername="postgresql").render_as_string(hide_password=False)
    run_id = "local_" + uuid.uuid4().hex[:16]
    schema = "proof_" + run_id
    args.output.mkdir(parents=True, exist_ok=False)
    scratch = Path(tempfile.mkdtemp(prefix="healthcare-proof-"))
    proof = {
        "run_id": run_id,
        "schema": schema,
        "boundary": "local_postgresql_service",
        "started_at": started_at,
        "phases": [],
        "phase_windows": [],
    }
    admin = None
    created = False
    owned_groups = {}
    cleanup_errors = []

    async def cleanup_group(pgid):
        """Retire successfully reaped group IDs while recording failures for a final retry."""
        process, output_task = owned_groups[pgid]
        try:
            await reap_phase_group(process, output_task)
        except Exception as exc:
            cleanup_errors.append(f"process:{pgid}:{type(exc).__name__}")
            return False
        # Retire IDs immediately so later cleanup never signals completed groups.
        del owned_groups[pgid]
        return True

    try:
        admin = await asyncpg.connect(native, timeout=5)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        await admin.close()
        snapshot = scratch / "source-snapshot"
        proof["source_snapshot"] = capture_source_snapshot(snapshot)
        manifests = {}
        for revision in ("v1", "v2"):
            manifests[revision] = compile_revision(revision, scratch / revision, source_package=snapshot)
        # Reject extra differences before executing either tree.
        if all(isinstance(m, dict) for m in manifests.values()):
            changed = [
                key for key, value in manifests["v1"]["files"].items() if manifests["v2"]["files"].get(key) != value
            ]
            assert changed == ["revision/write.py"]
            proof["revisions"] = manifests
        plans = [
            ("setup", "normal", "v1"),
            ("insert", "normal", "v1"),
            ("insert", "fault", "v2"),
            ("insert", "restore", "v1"),
        ]
        for case, phase, revision in plans:
            destination = args.output / f"{case}-{phase}.json"
            env = {**os.environ, "DATABASE_URL": dsn, "PYTHONPATH": str(scratch / revision)}
            window = {
                "case": case,
                "phase": phase,
                "revision": revision,
                "started_at": datetime.now(UTC).isoformat(),
                "completed_at": None,
            }
            proof["phase_windows"].append(window)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(snapshot / "demo" / "local_worker.py"),
                "--case",
                case,
                "--phase",
                phase,
                "--schema",
                schema,
                "--run-id",
                run_id,
                "--output",
                str(destination),
                "--expected-port",
                str(args.expected_port),
                env=env,
                cwd=scratch,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output_task = asyncio.create_task(process.communicate())
            owned_groups[process.pid] = (process, output_task)
            try:
                await asyncio.wait_for(wait_for_phase_exit(process), 120)
            finally:
                try:
                    cleaned = await cleanup_group(process.pid)
                finally:
                    window["completed_at"] = datetime.now(UTC).isoformat()
            if not cleaned:
                raise RuntimeError(f"Proof phase {case}/{phase} process group cleanup failed")
            if process.returncode:
                raise RuntimeError(f"Proof phase {case}/{phase} failed; inspect its sanitized JSON")
            result = json.loads(destination.read_text())
            result["process_window"] = dict(window)
            if case != "setup":
                proof["phases"].append(result)
            print(json.dumps({"case": case, "phase": phase, "revision": revision, "completed": True}), flush=True)
        proof["checks"] = check_proof(proof["phases"])
    except BaseException as exc:
        if proof["phase_windows"] and proof["phase_windows"][-1]["completed_at"] is None:
            proof["phase_windows"][-1]["completed_at"] = datetime.now(UTC).isoformat()
        proof["failure_type"] = type(exc).__name__
        raise
    finally:
        # Independent cleanup steps ensure one failure never skips the rest.
        for pgid in tuple(owned_groups):
            await cleanup_group(pgid)
        if admin is not None and not admin.is_closed():
            try:
                await admin.close()
            except Exception as exc:
                cleanup_errors.append("admin_connection:" + type(exc).__name__)
        if created:
            try:
                cleanup = await asyncpg.connect(native, timeout=5)
                try:
                    await cleanup.execute("SET lock_timeout = '5s'")
                    await cleanup.execute(f'DROP SCHEMA "{schema}" CASCADE')
                    proof["schema_removed"] = not await cleanup.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)", schema
                    )
                finally:
                    await cleanup.close()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        try:
            shutil.rmtree(scratch)
        except Exception as exc:
            cleanup_errors.append("source_tree:" + type(exc).__name__)
        proof["cleanup_errors"] = cleanup_errors
        proof["completed_at"] = datetime.now(UTC).isoformat()
        (args.output / "evidence.json").write_text(json.dumps(proof, indent=2, default=str))
        if cleanup_errors:
            raise RuntimeError("Owned fixture cleanup failed")


async def cancellable_run(args):
    """SIGTERM/SIGINT cancel work through finally rather than abandoning locks."""
    task = asyncio.create_task(run(args))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--expected-port", type=int, default=15439)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(cancellable_run(args))
    except (Exception, asyncio.CancelledError) as exc:
        print(json.dumps({"event": "proof_failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
