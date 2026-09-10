from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import structlog

from headless_codex.adapters.secondary.codex.codex_harness import (
    ANALYSIS_PROFILE,
    ANALYSIS_RCA_PROFILE,
    ANALYSIS_REPORT_PROFILE,
    MODEL_EVAL_PROFILE,
    MODEL_EVAL_RCA_PROFILE,
    MODEL_EVAL_REPORT_PROFILE,
    codex_environment,
    codex_exec_args,
    prepare_codex_home,
    prepare_workspace,
    runtime_home_root,
)
from headless_codex.config.settings import CODEX_TIMEOUT_SECONDS
from headless_codex.ports.dto.models import CodexResult
from headless_codex.ports.interfaces.codex_runner import CodexRunnerPort
from headless_codex.services.analysis_contract import AnalysisContractError, validate_analysis_completion
from headless_codex.services.execution_context import (
    ATTEMPT_ENV,
    CLAIM_TOKEN_ENV,
    RCA_ID_ENV,
    RUN_TOKEN_ENV,
    artifact_dir_for_token,
)

logger = structlog.get_logger()

_CANCEL_CHECK_INTERVAL = 15


def _watch_cancel(
    proc: subprocess.Popen,
    stop_event: Event,
    cancel_checker: Callable[[], bool],
) -> None:
    while not stop_event.wait(_CANCEL_CHECK_INTERVAL):
        if cancel_checker():
            logger.info("cancel_detected_killing_codex_process")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return


def _last_agent_message(stdout: str) -> str:
    result = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            result = item["text"]
    return result


class CodexSubprocessRunner(CodexRunnerPort):
    def run(
        self,
        prompt: str,
        *,
        execution_token: str,
        profile: str = ANALYSIS_PROFILE,
        report_prompt: str | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        rca_id: str | None = None,
        claim_token: str | None = None,
        attempt: int | None = None,
    ) -> CodexResult:
        """Run sequential specialists under one deadline and hand off replay-verified judgments."""
        if profile not in {ANALYSIS_PROFILE, MODEL_EVAL_PROFILE}:
            return self._run_single(
                prompt,
                execution_token=execution_token,
                profile=profile,
                cancel_checker=cancel_checker,
                rca_id=rca_id,
                claim_token=claim_token,
                attempt=attempt,
            )

        deadline = time.monotonic() + CODEX_TIMEOUT_SECONDS
        rca_profile = ANALYSIS_RCA_PROFILE if profile == ANALYSIS_PROFILE else MODEL_EVAL_RCA_PROFILE
        report_profile = ANALYSIS_REPORT_PROFILE if profile == ANALYSIS_PROFILE else MODEL_EVAL_REPORT_PROFILE
        rca_result = self._run_single(
            prompt + "\n\n런타임 역할: RCA 전문 프로세스다. 다른 에이전트를 위임하지 말고 "
            "RCA 분석 산출물만 저장한 뒤 전체 RCA 요약을 반환한다.",
            execution_token=execution_token,
            profile=rca_profile,
            cancel_checker=cancel_checker,
            rca_id=rca_id,
            claim_token=claim_token,
            attempt=attempt,
            deadline=deadline,
        )
        if not rca_result.success or rca_result.cancelled:
            return rca_result

        try:
            analysis = validate_analysis_completion(artifact_dir_for_token(execution_token))
            effective_state = json.dumps(analysis.effective_state_view(), ensure_ascii=False)
        except (AnalysisContractError, ValueError) as exc:
            return CodexResult(
                success=False,
                result=f"RCA effective state could not be verified: {exc}",
                raw_output=rca_result.raw_output,
            )

        report_result = self._run_single(
            (report_prompt if report_prompt is not None else prompt)
            + "\n\n런타임 역할: Report 전문 프로세스다. 다른 에이전트를 위임하지 말고 "
            "아래 RCA 전문 프로세스의 결과를 근거로 report.md와 playbook.json만 저장한다."
            + "\n\n[RCA 전문 프로세스 결과]\n"
            + rca_result.result
            + "\n\n[서버 검증 유효 상태 — 판정의 권위]\n"
            + effective_state
            + "\n이 상태의 selected_hypothesis_id, 판정, 실제 reasoning/evidence를 따른다. "
            "위 요약이 충돌하면 이 상태가 우선한다. closed는 rejected가 아니다.",
            execution_token=execution_token,
            profile=report_profile,
            cancel_checker=cancel_checker,
            rca_id=rca_id,
            claim_token=claim_token,
            attempt=attempt,
            deadline=deadline,
        )
        return CodexResult(
            success=report_result.success,
            result=report_result.result,
            raw_output=rca_result.raw_output + "\n" + report_result.raw_output,
            cancelled=report_result.cancelled,
        )

    def _run_single(
        self,
        prompt: str,
        *,
        execution_token: str,
        profile: str = ANALYSIS_PROFILE,
        cancel_checker: Callable[[], bool] | None = None,
        rca_id: str | None = None,
        claim_token: str | None = None,
        attempt: int | None = None,
        deadline: float | None = None,
    ) -> CodexResult:
        def timed_out() -> CodexResult:
            return CodexResult(
                success=False,
                result=f"Codex timed out after {CODEX_TIMEOUT_SECONDS}s",
                raw_output="",
            )

        if deadline is not None and time.monotonic() >= deadline:
            return timed_out()
        artifact_dir_for_token(execution_token)

        with (
            TemporaryDirectory(prefix="codex-workspace-") as workspace,
            TemporaryDirectory(prefix="codex-home-", dir=runtime_home_root()) as home,
        ):
            workspace_path = Path(workspace)
            home_path = Path(home)
            extra_env = {RUN_TOKEN_ENV: execution_token}
            if rca_id:
                extra_env[RCA_ID_ENV] = rca_id
            if claim_token:
                extra_env[CLAIM_TOKEN_ENV] = claim_token
            if attempt is not None:
                extra_env[ATTEMPT_ENV] = str(attempt)
            prepare_workspace(workspace_path, profile)
            config_path = prepare_codex_home(home_path, profile, extra_env)
            last_message = home_path / "last-message.txt"
            args = codex_exec_args(workspace_path, last_message)

            # Preparing the second workspace also consumes the shared analysis budget.
            if deadline is not None and time.monotonic() >= deadline:
                return timed_out()
            logger.info("codex_cli_started", profile=profile, config=str(config_path))

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
                timeout = CODEX_TIMEOUT_SECONDS if deadline is None else max(0, deadline - time.monotonic())
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                stop_event.set()
                return timed_out()
            finally:
                stop_event.set()

            result = last_message.read_text().strip() if last_message.is_file() else _last_agent_message(stdout or "")

        logger.info(
            "codex_cli_finished",
            rc=proc.returncode,
            stdout_bytes=len(stdout or ""),
            stderr_bytes=len(stderr or ""),
        )
        if stderr:
            logger.info("codex_cli_stderr", stderr=stderr[:5000])

        if proc.returncode == -15:
            return CodexResult(success=False, result="Process terminated (cancelled)", raw_output="", cancelled=True)
        if proc.returncode != 0:
            logger.error("codex_cli_failed", rc=proc.returncode, stdout=(stdout or "")[:5000])
            return CodexResult(
                success=False,
                result=f"Codex process error (rc={proc.returncode})",
                raw_output=stdout or stderr or "",
            )

        return CodexResult(success=True, result=result or (stdout or "").strip(), raw_output=stdout or "")
