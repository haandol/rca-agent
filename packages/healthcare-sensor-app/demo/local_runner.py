"""Normal -> fault -> restore proofs using only an explicit local PostgreSQL DSN.

Creates one unique schema, compiles independent source trees, launches service
phases, writes JSON evidence, then drops only that schema in finally. Never starts
or stops Docker, changes database roles, or falls back to default app settings.
"""

import argparse
import asyncio
import json
import math
import os
import shutil
import signal
import statistics
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
    can be delayed by a maintenance descendant after the phase leader crashes.
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
    """Fail the run when observations do not establish all four real mechanisms."""
    by_key = {(p["case"], p["phase"]): p for p in phases}
    assert len({phase["source"]["base_fingerprint"] for phase in phases}) == 1
    for phase in phases:
        bounds = [
            phase["process_window"]["started_at"],
            phase["started_at"],
            phase["measurement_started_at"],
            phase["measurement_completed_at"],
            phase["completed_at"],
            phase["process_window"]["completed_at"],
        ]
        parsed = [datetime.fromisoformat(value) for value in bounds]
        assert all(value.utcoffset().total_seconds() == 0 for value in parsed)
        assert parsed == sorted(parsed)
        for event in phase["events"]:
            assert parsed[1] <= datetime.fromisoformat(event["timestamp"]) <= parsed[-2]

        def check_operations(value, measurement_start=parsed[2], measurement_end=parsed[3]):
            """Require each nested operation's UTC interval to lie inside its measured phase."""
            if isinstance(value, dict):
                if isinstance(value.get("operation"), dict):
                    begin = datetime.fromisoformat(value["started_at"])
                    end = datetime.fromisoformat(value["completed_at"])
                    assert begin.utcoffset().total_seconds() == end.utcoffset().total_seconds() == 0
                    assert measurement_start <= begin <= end <= measurement_end
                for child in value.values():
                    check_operations(child)
            elif isinstance(value, list):
                for child in value:
                    check_operations(child)

        check_operations(phase)
    for case in ("pool", "query", "exception", "lock", "cancel"):
        for phase in ("normal", "fault", "restore"):
            proof = by_key[case, phase]
            assert proof["owned_sessions_after_dispose"] == 0
            assert proof["remaining_owned_schema_locks"] == 0
    for phase in ("normal", "restore"):
        assert all(w["outcome"] == "ok" for w in by_key["pool", phase]["writes"])
        assert by_key["exception", phase]["subsequent_write"]["outcome"] == "ok"
        assert all(p["checked_out_after_request"] == 0 for p in by_key["exception", phase]["probes"])
        assert by_key["cancel", phase]["checked_out_after_cancel"] == 0
        assert by_key["lock", phase]["write"]["outcome"] == "ok"
    assert any(w["error_type"] == "TimeoutError" for w in by_key["pool", "fault"]["writes"])
    hashes = set()
    for phase in ("normal", "fault", "restore"):
        for query in by_key["query", phase]["queries"]:
            assert query["outcome"] == "ok" and query["row_count"] == 120
            assert query["operation"]["sql_count"] == (121 if phase == "fault" else 1)
            hashes.add(query["rows_sha256"])
    assert len(hashes) == 1
    query_medians = {
        phase: statistics.median(q["elapsed_ms"] for q in by_key["query", phase]["queries"])
        for phase in ("normal", "fault", "restore")
    }
    assert query_medians["fault"] > max(query_medians["normal"], query_medians["restore"])
    fault = by_key["exception", "fault"]
    assert [p["checked_out_after_request"] for p in fault["probes"]] == [1, 2, 3]
    assert all(p["error_type"] == "DBAPIError" for p in fault["probes"])
    assert fault["subsequent_write"]["error_type"] == "TimeoutError"
    assert by_key["cancel", "fault"]["checked_out_after_cancel"] == 1
    lock = by_key["lock", "fault"]
    assert lock["blocked_write"]["outcome"] == "error"
    assert lock["restored_write"]["outcome"] == "ok"
    assert lock["maintenance_exit_code"] == 0
    assert any(e["event"] == "maintenance_released" for e in lock["release_events"])
    assert any(e.get("release_reason") == "sigterm" for e in lock["release_events"])
    assert [e["event"] for e in lock["bounded_hold_events"]] == ["maintenance_lock_acquired", "maintenance_released"]
    assert lock["bounded_hold_events"][-1]["release_reason"] == "hold_expired"
    assert any(lock["maintenance"]["backend_pid"] in row["blocking_pids"] for row in lock["snapshot"]["activity"])
    assert any(
        lock["maintenance"]["backend_pid"] in row["blocking_pids"]
        for snapshot in lock["events"]
        if snapshot.get("event") == "db_wait_snapshot"
        for row in snapshot["activity"]
    )
    return {
        "all_four_mechanisms": True,
        "cancellation_cleanup": True,
        "owned_resource_cleanup": True,
        "utc_phase_windows": True,
        "utc_operation_windows": True,
        "common_source_base": True,
    }


def calibrate_local_query_threshold(phases: list[dict]) -> dict:
    """Calibrate a LOCAL, same-run candidate from unchanged request timings.

    This is an in-sample comparison, not independent validation or an AWS alarm
    evaluation. Require separation of every recorded healthy/fault request;
    never round or alter raw samples to manufacture a passing threshold.
    """
    queries = {phase["phase"]: phase for phase in phases if phase["case"] == "query"}
    if set(queries) != {"normal", "fault", "restore"}:
        raise ValueError("Calibration requires all three actual query phases")
    workload = queries["normal"]["query_input"]
    if any(phase["query_input"] != workload for phase in queries.values()):
        raise ValueError("Query inputs differ across calibration phases")
    samples = {name: [q["elapsed_ms"] for q in phase["queries"]] for name, phase in queries.items()}
    if any(not values or any(not math.isfinite(value) or value < 0 for value in values) for values in samples.values()):
        raise ValueError("Calibration requires finite, nonnegative measured latencies")
    if any(len(values) != workload["requests"] for values in samples.values()):
        raise ValueError("Recorded query counts differ from the shared workload")
    healthy_max = max(*samples["normal"], *samples["restore"])
    fault_min = min(samples["fault"])
    if healthy_max >= fault_min:
        raise ValueError("No threshold separates all recorded healthy and fault requests")
    threshold = healthy_max + (fault_min - healthy_max) / 2
    if not healthy_max < threshold < fault_min:
        raise ValueError("No representable threshold strictly separates these samples")
    comparisons = {
        name: {
            "measurement_started_at": queries[name]["measurement_started_at"],
            "measurement_completed_at": queries[name]["measurement_completed_at"],
            "samples_ms": values,
            "sample_count": len(values),
            "above_threshold_count": sum(value > threshold for value in values),
            "below_threshold_count": sum(value < threshold for value in values),
        }
        for name, values in samples.items()
    }
    return {
        "scope": "LOCAL_ONLY: this PostgreSQL fixture and identical query workload",
        "metric": "local.patient_vitals.service_call_elapsed_ms",
        "unit": "Milliseconds",
        "comparison": "individual request latency > threshold_ms",
        "threshold_ms": threshold,
        "method": "midpoint between max(normal + restore) and min(fault)",
        "calibration_dataset": "same_run; not an independent validation dataset",
        "calibrated_at": datetime.now(UTC).isoformat(),
        "aws_alarm_evaluated": False,
        "aws_threshold_calibrated": False,
        "query_input": workload,
        "healthy_max_ms": healthy_max,
        "fault_min_ms": fault_min,
        "phases": comparisons,
        "relationship_verified": (
            comparisons["normal"]["below_threshold_count"] == comparisons["normal"]["sample_count"]
            and comparisons["restore"]["below_threshold_count"] == comparisons["restore"]["sample_count"]
            and comparisons["fault"]["above_threshold_count"] == comparisons["fault"]["sample_count"]
        ),
    }


async def run(args):
    """Execute frozen revisions against the owned fixture and preserve evidence through cleanup."""
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
        for revision in ("r1", "r2", "r3"):
            compile_revision(revision, scratch / revision, source_package=snapshot)
        plans = [("setup", "normal", "r1")]
        plans += [
            (case, phase, revision if phase == "fault" else "r1")
            for case, revision in (
                ("query", "r2"),
                ("pool", "r1"),
                ("lock", "r1"),
                ("exception", "r3"),
                ("cancel", "r3"),
            )
            for phase in ("normal", "fault", "restore")
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
        proof["local_query_calibration"] = calibrate_local_query_threshold(proof["phases"])
        proof["checks"]["identical_query_inputs"] = True
        proof["checks"]["local_query_threshold_relation"] = proof["local_query_calibration"]["relationship_verified"]
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
        if "local_query_calibration" in proof:
            (args.output / "local-query-calibration.json").write_text(
                json.dumps(proof["local_query_calibration"], indent=2)
            )
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
    parser.add_argument("--expected-port", type=int, default=32768)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(cancellable_run(args))
    except (Exception, asyncio.CancelledError) as exc:
        print(json.dumps({"event": "proof_failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
