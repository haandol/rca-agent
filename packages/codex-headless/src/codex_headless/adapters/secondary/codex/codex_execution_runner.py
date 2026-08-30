from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import structlog

from codex_headless.adapters.secondary.codex.codex_harness import (
    EXECUTION_PROFILE,
    RETROSPECTIVE_PROFILE,
    codex_environment,
    codex_exec_args,
    prepare_codex_home,
    prepare_workspace,
    runtime_home_root,
)
from codex_headless.adapters.secondary.codex.codex_subprocess_runner import _last_agent_message
from codex_headless.config.settings import (
    EXECUTION_TIMEOUT_SECONDS,
    RETROSPECTIVE_TIMEOUT_SECONDS,
)
from codex_headless.ports.dto.models import CodexResult
from codex_headless.ports.interfaces.execution_runner import ExecutionRunnerPort
from codex_headless.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    APPROVED_SUCCESS_CRITERIA_ENV,
    EXECUTION_ID_ENV,
    EXECUTION_TOKEN_ENV,
    workspace_for_token,
)

logger = structlog.get_logger()

_CANCEL_CHECK_INTERVAL = 15


def _watch_cancel(proc: subprocess.Popen, stop_event: Event, cancel_checker: Callable[[], bool]) -> None:
    while not stop_event.wait(_CANCEL_CHECK_INTERVAL):
        if cancel_checker():
            logger.info("execution_cancel_detected")
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
    ) -> CodexResult:
        return self._run(
            prompt,
            profile=RETROSPECTIVE_PROFILE,
            execution_token=execution_token,
            execution_id=execution_id,
            timeout_seconds=RETROSPECTIVE_TIMEOUT_SECONDS,
            cancel_checker=None,
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

        with (
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
            prepare_workspace(workspace_path, profile)
            config_path = prepare_codex_home(home_path, profile, extra_env)
            last_message = home_path / "last-message.txt"
            args = codex_exec_args(workspace_path, last_message)

            logger.info("execution_cli_started", profile=profile, execution_id=execution_id, config=str(config_path))

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

            stop_event = Event()
            if cancel_checker:
                Thread(target=_watch_cancel, args=(proc, stop_event, cancel_checker), daemon=True).start()

            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                stop_event.set()
                return CodexResult(
                    success=False,
                    result=f"Codex timed out after {timeout_seconds}s",
                    raw_output="",
                )
            finally:
                stop_event.set()

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
