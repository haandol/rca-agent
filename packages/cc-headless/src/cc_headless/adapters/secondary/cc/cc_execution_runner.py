from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import structlog

from cc_headless.config.settings import (
    EXECUTION_TIMEOUT_SECONDS,
    RETROSPECTIVE_TIMEOUT_SECONDS,
)
from cc_headless.ports.dto.models import CcResult
from cc_headless.ports.interfaces.execution_runner import ExecutionRunnerPort
from cc_headless.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    APPROVED_SUCCESS_CRITERIA_ENV,
    EXECUTION_ID_ENV,
    EXECUTION_TOKEN_ENV,
    workspace_for_token,
)

logger = structlog.get_logger()

_CANCEL_CHECK_INTERVAL = 15


def _find_file(name: str) -> str:
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        candidate = parent / name
        if candidate.exists():
            return str(candidate)
    return f"/app/{name}"


_MCP_CONFIG_PATH = _find_file("execution-mcp-config.json")
_WORKSPACE_SOURCE = Path(_MCP_CONFIG_PATH).parent
_PACKAGE_ROOT_PLACEHOLDER = "{{PACKAGE_ROOT}}"

# MCP 도구는 위임된 서브 에이전트에서만 해석된다. 루트 에이전트의 도구 목록에 적으면
# 노출되지 않아, 실행 에이전트가 `Skill` 하나만 들고 아무 절차도 수행하지 못한다 —
# 라이브 실측에서 확인한 동작이다. 그래서 루트는 위임자이고 실제 도구는 하위가 든다.
_EXECUTION_AGENT = "execution-orchestrator"
_RETROSPECTIVE_AGENT = "retrospective-orchestrator"
_BUILTIN_TOOLS = ("Agent", "Skill")

# 실행 하네스의 쓰기 경로는 이 세 도구뿐이다. Bash 도, 임의 HTTP 도 없다 — 명령은
# 서버가 파싱해 파괴성을 판정한 뒤에만 실행된다.
_EXECUTION_TOOLS = (
    *_BUILTIN_TOOLS,
    "mcp__cloudwatch__*",
    "mcp__playbook-execution__run_playbook_command",
    "mcp__playbook-execution__record_step_outcome",
    "mcp__playbook-execution__record_resolution",
)
# 회고는 실행 증거를 읽고 갱신안을 쓸 뿐이므로 실행 도구를 갖지 않는다.
_RETROSPECTIVE_TOOLS = (
    *_BUILTIN_TOOLS,
    "mcp__playbook-retrospective__save_playbook_update",
)


def _render_mcp_config(source: str, dest_dir: Path) -> str:
    text = Path(source).read_text()
    if _PACKAGE_ROOT_PLACEHOLDER not in text:
        return source
    dest = dest_dir / "execution-mcp-config.json"
    dest.write_text(text.replace(_PACKAGE_ROOT_PLACEHOLDER, str(_WORKSPACE_SOURCE)))
    return str(dest)


def _prepare_workspace(path: Path) -> None:
    """실행 하네스의 지침을 작업 디렉터리에 놓는다.

    분석 하네스의 `CLAUDE.md` 는 읽기 전용을 선언하므로 실행에 쓸 수 없다. 실행은
    자신의 지침 파일을 쓴다.
    """
    guidance = _WORKSPACE_SOURCE / "EXECUTION.md"
    agents = _WORKSPACE_SOURCE / ".claude-execution"
    if guidance.is_file():
        shutil.copy2(guidance, path / "CLAUDE.md")
    if agents.is_dir():
        shutil.copytree(agents, path / ".claude")


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


def _parse_result(stdout: str) -> CcResult:
    try:
        parsed = json.loads(stdout)
        result = parsed.get("result") or parsed.get("data", {}).get("result") or stdout
        if not isinstance(result, str):
            result = json.dumps(result)
        return CcResult(success=True, result=result, raw_output=stdout)
    except (json.JSONDecodeError, AttributeError):
        return CcResult(success=True, result=stdout.strip(), raw_output=stdout)


class CcExecutionRunner(ExecutionRunnerPort):
    def run_execution(
        self,
        prompt: str,
        *,
        execution_token: str,
        execution_id: str,
        approved_step_ids: tuple[str, ...],
        approved_success_criteria: dict[str, str],
        cancel_checker: Callable[[], bool] | None = None,
    ) -> CcResult:
        return self._run(
            prompt,
            execution_token=execution_token,
            execution_id=execution_id,
            approved_step_ids=approved_step_ids,
            approved_success_criteria=approved_success_criteria,
            agent=_EXECUTION_AGENT,
            allowed_tools=_EXECUTION_TOOLS,
            timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
            cancel_checker=cancel_checker,
        )

    def run_retrospective(
        self,
        prompt: str,
        *,
        execution_token: str,
        execution_id: str,
    ) -> CcResult:
        return self._run(
            prompt,
            execution_token=execution_token,
            execution_id=execution_id,
            agent=_RETROSPECTIVE_AGENT,
            allowed_tools=_RETROSPECTIVE_TOOLS,
            timeout_seconds=RETROSPECTIVE_TIMEOUT_SECONDS,
            cancel_checker=None,
        )

    def _run(
        self,
        prompt: str,
        *,
        execution_token: str,
        execution_id: str,
        agent: str,
        allowed_tools: tuple[str, ...],
        timeout_seconds: int,
        cancel_checker: Callable[[], bool] | None,
        approved_step_ids: tuple[str, ...] = (),
        approved_success_criteria: dict[str, str] | None = None,
    ) -> CcResult:
        workspace_for_token(execution_token)

        with (
            TemporaryDirectory(prefix="cc-exec-workspace-") as workspace,
            TemporaryDirectory(prefix="cc-exec-home-") as home,
        ):
            workspace_path = Path(workspace)
            _prepare_workspace(workspace_path)
            resolved_mcp_config = _render_mcp_config(_MCP_CONFIG_PATH, Path(home))
            args = [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--dangerously-skip-permissions",
                "--mcp-config",
                resolved_mcp_config,
                "--strict-mcp-config",
                "--no-session-persistence",
                "--agent",
                agent,
                "--tools",
                ",".join(_BUILTIN_TOOLS),
                "--allowedTools",
                ",".join(allowed_tools),
            ]

            logger.info("execution_cli_started", agent=agent, execution_id=execution_id)

            env = {
                **os.environ,
                "HOME": home,
                "CLAUDE_CONFIG_DIR": str(Path(home) / ".claude"),
                EXECUTION_TOKEN_ENV: execution_token,
                EXECUTION_ID_ENV: execution_id,
                APPROVED_STEP_IDS_ENV: json.dumps(approved_step_ids),
                APPROVED_SUCCESS_CRITERIA_ENV: json.dumps(approved_success_criteria or {}, ensure_ascii=False),
            }

            try:
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=workspace,
                    env=env,
                )
            except FileNotFoundError:
                return CcResult(
                    success=False,
                    result="Claude Code CLI not found. Ensure @anthropic-ai/claude-code is installed globally.",
                    raw_output="",
                )

            stop_event = Event()
            if cancel_checker:
                Thread(target=_watch_cancel, args=(proc, stop_event, cancel_checker), daemon=True).start()

            try:
                stdout, stderr = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                stop_event.set()
                return CcResult(
                    success=False,
                    result=f"Claude Code timed out after {timeout_seconds}s",
                    raw_output="",
                )
            finally:
                stop_event.set()

        logger.info("execution_cli_finished", rc=proc.returncode, agent=agent)
        if stderr:
            logger.info("execution_cli_stderr", stderr=stderr[:5000])

        if proc.returncode == -15:
            return CcResult(success=False, result="Process terminated (cancelled)", raw_output="", cancelled=True)
        if proc.returncode != 0:
            logger.error("execution_cli_failed", rc=proc.returncode, stdout=(stdout or "")[:5000])
            return CcResult(
                success=False,
                result=f"Claude Code process error (rc={proc.returncode})",
                raw_output=stdout or stderr or "",
            )

        return _parse_result(stdout or "")
