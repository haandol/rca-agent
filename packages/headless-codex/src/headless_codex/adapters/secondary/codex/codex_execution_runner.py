from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread

import structlog

from headless_codex.adapters.secondary.codex.codex_harness import (
    EXECUTION_PROFILE,
    RETROSPECTIVE_PROFILE,
    codex_environment,
    codex_exec_args,
    prepare_codex_home,
    prepare_workspace,
    runtime_home_root,
)
from headless_codex.adapters.secondary.codex.codex_subprocess_runner import _last_agent_message
from headless_codex.config.settings import (
    EXECUTION_TIMEOUT_SECONDS,
    RETROSPECTIVE_TIMEOUT_SECONDS,
)
from headless_codex.ports.dto.models import CodexResult
from headless_codex.ports.interfaces.execution_runner import ExecutionRunnerPort
from headless_codex.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    APPROVED_SUCCESS_CRITERIA_ENV,
    EXECUTION_DEADLINE_ENV,
    EXECUTION_ID_ENV,
    EXECUTION_TOKEN_ENV,
    observation_control_path_for_token,
    workspace_for_token,
    write_observation_json,
)

logger = structlog.get_logger()

_CANCEL_CHECK_INTERVAL = 5


class _ObservationControl:
    """Publish token-scoped, short-lived authorization without extending the runner budget."""

    def __init__(self, token: str, execution_id: str, deadline_epoch: float, deadline_monotonic: float):
        self.path = observation_control_path_for_token(token)
        self.execution_id = execution_id
        self.deadline_epoch = deadline_epoch
        self.deadline_monotonic = deadline_monotonic
        self.stop_event = Event()
        self._lock = Lock()
        self._publish(active=False, checked_at=time.time())

    def remaining(self) -> float:
        return max(0.0, min(self.deadline_epoch - time.time(), self.deadline_monotonic - time.monotonic()))

    def _publish(self, *, active: bool, checked_at: float) -> bool:
        try:
            write_observation_json(
                self.path,
                {
                    "execution_id": self.execution_id,
                    "deadline_epoch": self.deadline_epoch,
                    "checked_at_epoch": checked_at,
                    "active": active,
                },
            )
            return True
        except OSError:
            # Never refresh permission after a failed write. If removal also fails, the
            # existing heartbeat expires; cleanup must not replace the runner's outcome.
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                logger.exception("execution_control_cleanup_failed")
            logger.exception("execution_control_publish_failed")
            return False

    def refresh(self, cancel_checker: Callable[[], bool] | None) -> bool:
        checked_at = time.time()
        check_started = time.monotonic()
        try:
            valid = (
                not self.stop_event.is_set()
                and self.remaining() > 0
                and cancel_checker is not None
                and not cancel_checker()
            )
        except Exception:
            logger.exception("execution_control_check_failed")
            valid = False
        with self._lock:
            active = (
                valid
                and not self.stop_event.is_set()
                and self.remaining() > 0
                and time.monotonic() - check_started < _CANCEL_CHECK_INTERVAL
            )
            if not active:
                self.stop_event.set()
            if not self._publish(active=active, checked_at=checked_at):
                self.stop_event.set()
                return False
            return active

    def deactivate(self) -> None:
        # Share this lock with refresh so a late callback cannot reactivate a finished run.
        with self._lock:
            self.stop_event.set()
            self._publish(active=False, checked_at=time.time())


def _watch_cancel(
    proc: subprocess.Popen, control: _ObservationControl, cancel_checker: Callable[[], bool] | None
) -> None:
    next_check = time.monotonic() + _CANCEL_CHECK_INTERVAL
    while not control.stop_event.wait(min(max(0.0, next_check - time.monotonic()), control.remaining())):
        next_check = time.monotonic() + _CANCEL_CHECK_INTERVAL
        if not control.refresh(cancel_checker):
            logger.info("execution_cancel_detected")
            control.deactivate()
            proc.terminate()
            try:
                proc.wait(timeout=min(10, control.remaining()))
            except subprocess.TimeoutExpired:
                proc.kill()
            return


def _watch_retrospective_cancel(proc: subprocess.Popen, stop_event: Event, cancel_checker: Callable[[], bool]) -> None:
    """Retrospective cancellation belongs to its own process, after execution is RESOLVED."""
    while not stop_event.wait(_CANCEL_CHECK_INTERVAL):
        if cancel_checker():
            logger.info("retrospective_cancel_detected")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return


class CodexExecutionRunner(ExecutionRunnerPort):
    def run_execution(
        self,
        prompt: str,
        *,
        execution_token: str,
        execution_id: str,
        approved_step_ids: tuple[str, ...],
        approved_success_criteria: dict[str, str],
        cancel_checker: Callable[[], bool] | None = None,
    ) -> CodexResult:
        return self._run(
            prompt,
            profile=EXECUTION_PROFILE,
            execution_token=execution_token,
            execution_id=execution_id,
            approved_step_ids=approved_step_ids,
            approved_success_criteria=approved_success_criteria,
            timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
            cancel_checker=cancel_checker,
        )

    def run_retrospective(
        self,
        prompt: str,
        *,
        execution_token: str,
        execution_id: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> CodexResult:
        return self._run(
            prompt,
            profile=RETROSPECTIVE_PROFILE,
            execution_token=execution_token,
            execution_id=execution_id,
            timeout_seconds=RETROSPECTIVE_TIMEOUT_SECONDS,
            cancel_checker=cancel_checker,
        )

    def _run(
        self,
        prompt: str,
        *,
        profile: str,
        execution_token: str,
        execution_id: str,
        timeout_seconds: int,
        cancel_checker: Callable[[], bool] | None,
        approved_step_ids: tuple[str, ...] = (),
        approved_success_criteria: dict[str, str] | None = None,
    ) -> CodexResult:
        workspace_for_token(execution_token)
        deadline_epoch = time.time() + timeout_seconds
        deadline_monotonic = time.monotonic() + timeout_seconds

        with (
            ExitStack() as cleanup,
            TemporaryDirectory(prefix=f"codex-{profile}-workspace-") as workspace,
            TemporaryDirectory(prefix=f"codex-{profile}-home-", dir=runtime_home_root()) as home,
        ):
            workspace_path = Path(workspace)
            home_path = Path(home)
            extra_env = {
                EXECUTION_TOKEN_ENV: execution_token,
                EXECUTION_ID_ENV: execution_id,
                APPROVED_STEP_IDS_ENV: json.dumps(approved_step_ids),
                APPROVED_SUCCESS_CRITERIA_ENV: json.dumps(approved_success_criteria or {}, ensure_ascii=False),
            }
            control = None
            if profile == EXECUTION_PROFILE:
                control = _ObservationControl(execution_token, execution_id, deadline_epoch, deadline_monotonic)
                cleanup.callback(control.deactivate)
                extra_env[EXECUTION_DEADLINE_ENV] = str(deadline_epoch)
            prepare_workspace(workspace_path, profile)
            config_path = prepare_codex_home(home_path, profile, extra_env)
            last_message = home_path / "last-message.txt"
            args = codex_exec_args(workspace_path, last_message)

            logger.info("execution_cli_started", profile=profile, execution_id=execution_id, config=str(config_path))

            if control is not None and not control.refresh(cancel_checker):
                return CodexResult(success=False, result="Execution control is inactive", raw_output="", cancelled=True)

            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=workspace,
                    env=codex_environment(home_path, extra_env),
                )
            except FileNotFoundError:
                return CodexResult(
                    success=False,
                    result="Codex CLI not found. Ensure @openai/codex is installed globally.",
                    raw_output="",
                )

            if control is not None:
                Thread(target=_watch_cancel, args=(proc, control, cancel_checker), daemon=True).start()
            retrospective_stop = Event()
            cleanup.callback(retrospective_stop.set)
            if control is None and cancel_checker is not None:
                Thread(
                    target=_watch_retrospective_cancel,
                    args=(proc, retrospective_stop, cancel_checker),
                    daemon=True,
                ).start()

            try:
                remaining = max(0.0, min(deadline_epoch - time.time(), deadline_monotonic - time.monotonic()))
                stdout, stderr = proc.communicate(input=prompt, timeout=remaining)
            except subprocess.TimeoutExpired:
                if control is not None:
                    control.deactivate()
                proc.kill()
                proc.wait()
                return CodexResult(
                    success=False,
                    result=f"Codex timed out after {timeout_seconds}s",
                    raw_output="",
                )
            finally:
                retrospective_stop.set()
                if control is not None:
                    control.deactivate()

            result = last_message.read_text().strip() if last_message.is_file() else _last_agent_message(stdout or "")

        logger.info("execution_cli_finished", rc=proc.returncode, profile=profile)
        if stderr:
            logger.info("execution_cli_stderr", stderr=stderr[:5000])

        if proc.returncode == -15:
            return CodexResult(success=False, result="Process terminated (cancelled)", raw_output="", cancelled=True)
        if proc.returncode != 0:
            logger.error("execution_cli_failed", rc=proc.returncode, stdout=(stdout or "")[:5000])
            return CodexResult(
                success=False,
                result=f"Codex process error (rc={proc.returncode})",
                raw_output=stdout or stderr or "",
            )

        return CodexResult(success=True, result=result or (stdout or "").strip(), raw_output=stdout or "")
