"""Real subprocess regressions for phase leaders that leave descendants alive."""

import asyncio
import importlib.util
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE / "demo"))
    spec = importlib.util.spec_from_file_location("orphan_proof_runner", PACKAGE / "demo/local_runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Real signaling/escalation, with short bounds for deterministic regressions.
    monkeypatch.setattr(module, "PROCESS_TERM_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(module, "PROCESS_KILL_TIMEOUT_SECONDS", 1.0)
    return module


class FixtureConnection:
    """Only fixture/schema bookkeeping is stubbed; process lifetime is real."""

    def __init__(self):
        self.closed = False
        self.commands = []

    async def execute(self, command):
        self.commands.append(command)

    async def fetchval(self, *_args):
        return False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("leader_exit", [0, 7])
async def test_reaps_orphan_after_success_or_crash_before_next_phase(runner, monkeypatch, tmp_path, leader_exit):
    await orphan_regression(runner, monkeypatch, tmp_path, leader_exit=leader_exit)


async def test_group_cleanup_failure_is_recorded_and_other_cleanup_continues(runner, monkeypatch, tmp_path):
    await orphan_regression(runner, monkeypatch, tmp_path, leader_exit=7, fail_cleanup_once=True)


async def orphan_regression(runner, monkeypatch, tmp_path, *, leader_exit, fail_cleanup_once=False):
    """An exited leader leaves a TERM-resistant child holding inherited pipes.

    A separate, test-owned control process must remain alive. Fake PostgreSQL
    bookkeeping lets this regression run without a database or credentials.
    """
    env_file = tmp_path / "database.env"
    env_file.write_text("DATABASE_URL=postgresql+asyncpg://unused:unused@127.0.0.1:32768/rca_demo\n")
    args = SimpleNamespace(env_file=env_file, expected_port=32768, output=tmp_path / "proof")
    connections = []

    async def connect(*_args, **_kwargs):
        connection = FixtureConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(runner.asyncpg, "connect", connect)
    compiled_trees = []
    monkeypatch.setattr(runner, "compile_revision", lambda _revision, path, **_kwargs: compiled_trees.append(path))
    real_spawn = asyncio.create_subprocess_exec
    control = await real_spawn(sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True)
    spawned = []
    orphan_pid_file = tmp_path / "orphan.pid"
    descendant = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(orphan_pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )

    async def spawn_phase(*command, **kwargs):
        assert kwargs["start_new_session"] is True
        if spawned:
            # A successful phase must reap its descendants before launching more.
            with pytest.raises(ProcessLookupError):
                os.killpg(spawned[0].pid, 0)
            code = "import os; os._exit(7)"
        else:
            destination = command[command.index("--output") + 1]
            code = (
                "import os,subprocess,sys,time; from pathlib import Path; "
                f"subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
                f"Path({destination!r}).write_text('{{}}')\n"
                f"while not Path({str(orphan_pid_file)!r}).exists(): time.sleep(0.01)\n"
                f"os._exit({leader_exit})"
            )
        process = await real_spawn(sys.executable, "-c", code, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn_phase)
    if fail_cleanup_once:
        real_reap = runner.reap_phase_group
        attempts = 0

        async def fail_then_reap(process, output_task):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated group cleanup failure")
            await real_reap(process, output_task)

        monkeypatch.setattr(runner, "reap_phase_group", fail_then_reap)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(runner.run(args), timeout=5)
        assert time.monotonic() - started < 5
        assert spawned[0].returncode == leader_exit
        assert orphan_pid_file.exists()
        for phase in spawned:
            with pytest.raises(ProcessLookupError):
                os.killpg(phase.pid, 0)
        assert control.returncode is None
        os.kill(control.pid, 0)
        evidence = json.loads((args.output / "evidence.json").read_text())
        assert evidence["schema_removed"] is True
        assert all(connection.closed for connection in connections)
        assert any(command.startswith("DROP SCHEMA") for connection in connections for command in connection.commands)
        assert compiled_trees and not compiled_trees[0].parent.exists()
        if fail_cleanup_once:
            assert evidence["cleanup_errors"] == [f"process:{spawned[0].pid}:OSError"]
        else:
            assert evidence["cleanup_errors"] == []
            assert len(spawned) == (2 if leader_exit == 0 else 1)
    finally:
        # Never target a group not created by this test, even if an assertion fails.
        for phase in spawned:
            with suppress(ProcessLookupError):
                os.killpg(phase.pid, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(phase.communicate(), 2)
        with suppress(ProcessLookupError):
            os.killpg(control.pid, signal.SIGKILL)
        await asyncio.wait_for(control.wait(), 2)
