"""플레이북 실행 MCP server.

실행 에이전트에만 제공되는 쓰기 경로다. 분석 실행은 이 서버를 갖지 않는다.

두 가지를 서버가 보유한다. 첫째, **파괴성 판정** — 에이전트가 어떤 명령을 요청하든
서버가 작업 이름을 추출해 거부 어휘와 대조한다. 둘째, **증거 기록** — 시도와 결과가
에이전트의 서술이 아니라 서버가 관측한 사실로 남는다.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from fastmcp import FastMCP

from cc_headless.services.command_gate import evaluate_command
from cc_headless.services.execution_evidence import (
    FailureClass,
    parse_failure_class,
    redact,
    redact_arguments,
)
from cc_headless.services.execution_workspace import (
    APPROVED_STEP_IDS_ENV,
    APPROVED_SUCCESS_CRITERIA_ENV,
    EXECUTION_TOKEN_ENV,
    evidence_path_for_token,
)

mcp = FastMCP("playbook-execution")

_COMMAND_TIMEOUT_SECONDS = int(os.environ.get("EXECUTION_COMMAND_TIMEOUT_SECONDS", "300"))
_MAX_OUTPUT_CHARS = 20_000


def _evidence_file() -> Path | None:
    token = os.environ.get(EXECUTION_TOKEN_ENV, "")
    try:
        path = evidence_path_for_token(token)
    except ValueError:
        return None
    return path if path.parent.is_dir() and not path.parent.is_symlink() else None


def _append_record(record: dict) -> bool:
    """증거를 append 한다. 서버가 기록의 권위를 가진다."""
    path = _evidence_file()
    if path is None:
        return False
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _approved_step_ids() -> frozenset[str]:
    try:
        parsed = json.loads(os.environ.get(APPROVED_STEP_IDS_ENV, ""))
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(parsed, list):
        return frozenset()
    return frozenset(value.strip() for value in parsed if isinstance(value, str) and value.strip())


def _approved_success_criteria() -> dict[str, str]:
    try:
        parsed = json.loads(os.environ.get(APPROVED_SUCCESS_CRITERIA_ENV, ""))
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        step_id.strip(): criterion
        for step_id, criterion in parsed.items()
        if isinstance(step_id, str) and step_id.strip() and isinstance(criterion, str) and criterion.strip()
    }


def _validate_step_id(step_id: str) -> str | None:
    if not isinstance(step_id, str) or not step_id.strip():
        return "step_id is required"
    if step_id.strip() not in _approved_step_ids():
        return "step_id is not declared in the approved playbook"
    return None


def _classify_exit(stderr: str, returncode: int) -> FailureClass:
    """종료 출력에서 실패 분류를 추정한다.

    분류의 목적은 회고가 절차 결함과 일시적 오류를 구분하는 것이다. 확정할 수 없으면
    UNKNOWN 으로 두어 회고가 교정 대상으로 삼지 않게 한다 — 잘못된 교정보다 미교정이
    안전하다.
    """
    if returncode == 0:
        return FailureClass.UNKNOWN
    lowered = stderr.lower()
    if "throttl" in lowered or "ratelimit" in lowered or "toomanyrequests" in lowered:
        return FailureClass.THROTTLED
    if "timed out" in lowered or "timeout" in lowered:
        return FailureClass.TIMEOUT
    if "accessdenied" in lowered or "not authorized" in lowered or "unauthorizedoperation" in lowered:
        return FailureClass.PERMISSION_DENIED
    if "notfound" in lowered or "does not exist" in lowered or "no such" in lowered:
        return FailureClass.TARGET_NOT_FOUND
    if (
        "validationerror" in lowered
        or "invalidparameter" in lowered
        or "unknown options" in lowered
        or "invalid choice" in lowered
        or "argument" in lowered
    ):
        return FailureClass.INVALID_ARGUMENT
    if "invalidstate" in lowered or "not in a valid state" in lowered or "precondition" in lowered:
        return FailureClass.MISSING_PRECONDITION
    return FailureClass.UNKNOWN


@mcp.tool()
def run_playbook_command(step_id: str, command: str, intent: str = "") -> str:
    """플레이북 절차의 한 명령을 실행한다. 파괴적·판정 불가 명령은 거부된다.

    Args:
        step_id: 수행 중인 플레이북 실행 절차의 식별자.
        command: 실행할 AWS CLI 명령 한 개. 셸 합성(파이프, 리다이렉션, `&&`,
                 명령 치환)은 판정 불가로 거부된다.
        intent: 이 명령이 절차의 무엇을 달성하려는지.
    """
    step_error = _validate_step_id(step_id)
    if step_error:
        return json.dumps({"ok": False, "error": step_error}, ensure_ascii=False)

    verdict = evaluate_command(command)
    safe_command = redact(command)

    if not verdict.allowed:
        failure_class = FailureClass.BLOCKED_UNDECIDABLE if verdict.undecidable else FailureClass.BLOCKED_DESTRUCTIVE
        _append_record(
            {
                "type": "attempt",
                "step_id": step_id.strip(),
                "intent": intent,
                "command": safe_command,
                "blocked": True,
                "block_reason": verdict.reason,
                "failure_class": str(failure_class),
                "succeeded": False,
                "exit_status": "blocked",
            }
        )
        # 차단은 실행 전체를 중단시키지 않는다. 해당 절차만 수동 조치로 남는다.
        return json.dumps(
            {
                "ok": False,
                "blocked": True,
                "reason": verdict.reason,
                "guidance": (
                    "This step stays a manual action. Do not retry it or work around the refusal. "
                    "Continue with the remaining steps."
                ),
            },
            ensure_ascii=False,
        )

    try:
        completed = subprocess.run(  # noqa: S603 - argv comes from the gate, never a shell string
            list(verdict.argv),
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _append_record(
            {
                "type": "attempt",
                "step_id": step_id.strip(),
                "intent": intent,
                "command": safe_command,
                "succeeded": False,
                "exit_status": "timeout",
                "failure_class": str(FailureClass.TIMEOUT),
                "error_output": f"command exceeded {_COMMAND_TIMEOUT_SECONDS}s",
            }
        )
        return json.dumps(
            {"ok": False, "error": f"command timed out after {_COMMAND_TIMEOUT_SECONDS}s"},
            ensure_ascii=False,
        )
    except OSError as exc:
        _append_record(
            {
                "type": "attempt",
                "step_id": step_id.strip(),
                "intent": intent,
                "command": safe_command,
                "succeeded": False,
                "exit_status": "spawn_failed",
                "failure_class": str(FailureClass.UNKNOWN),
                "error_output": redact(str(exc)),
            }
        )
        return json.dumps({"ok": False, "error": f"command could not start: {exc}"}, ensure_ascii=False)

    stdout = redact(completed.stdout or "")[:_MAX_OUTPUT_CHARS]
    stderr = redact(completed.stderr or "")[:_MAX_OUTPUT_CHARS]
    succeeded = completed.returncode == 0
    failure_class = _classify_exit(stderr, completed.returncode)

    _append_record(
        {
            "type": "attempt",
            "step_id": step_id.strip(),
            "intent": intent,
            "command": safe_command,
            "arguments": redact_arguments({"service": verdict.service, "operation": verdict.operation}),
            "succeeded": succeeded,
            "exit_status": str(completed.returncode),
            "failure_class": str(failure_class) if not succeeded else None,
            "error_output": stderr if not succeeded else "",
            "observation": stdout[:2000],
        }
    )

    return json.dumps(
        {
            "ok": succeeded,
            "exit_status": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "failure_class": str(failure_class) if not succeeded else None,
        },
        ensure_ascii=False,
    )


@mcp.tool()
def record_step_outcome(
    step_id: str,
    success_criteria: str,
    observation: str,
    criteria_met: bool,
    failure_class: str = "",
    manual_action_required: bool = False,
) -> str:
    """한 절차의 관측 결과를 증거에 기록한다.

    Args:
        step_id: 플레이북 실행 절차의 식별자.
        success_criteria: 이 절차가 성공을 판정하는 기준 — 플레이북에 적힌 그대로.
        observation: 그 기준을 관측한 결과. 어떤 지표가 어떤 값이었는지 쓴다.
        criteria_met: 관측이 기준을 만족했는지. 관측하지 못했으면 false 로 둔다.
        failure_class: 만족하지 못한 경우의 분류.
        manual_action_required: 이 절차가 사람의 조치로 남아야 하는지.
    """
    step_error = _validate_step_id(step_id)
    if step_error:
        return json.dumps({"ok": False, "error": step_error}, ensure_ascii=False)

    approved_criteria = _approved_success_criteria().get(step_id.strip())
    if approved_criteria is None:
        return json.dumps(
            {"ok": False, "error": "approved success_criteria is unavailable for this step"},
            ensure_ascii=False,
        )
    if success_criteria != approved_criteria:
        return json.dumps(
            {
                "ok": False,
                "error": "success_criteria does not exactly match the approved playbook",
            },
            ensure_ascii=False,
        )

    ok = _append_record(
        {
            "type": "step_outcome",
            "step_id": step_id.strip(),
            "success_criteria": approved_criteria,
            "observation": redact(observation),
            "criteria_met": bool(criteria_met),
            "failure_class": str(parse_failure_class(failure_class)) if failure_class else None,
            "manual_action_required": bool(manual_action_required),
        }
    )
    if not ok:
        return json.dumps({"ok": False, "error": "missing execution context"}, ensure_ascii=False)
    return json.dumps({"ok": True}, ensure_ascii=False)


@mcp.tool()
def record_resolution(observation: str, resolved: bool, unobservable_reason: str = "") -> str:
    """이슈 해소 여부의 관측 결과를 기록한다.

    관측으로 확정할 수 없으면 `resolved=false` 와 함께 `unobservable_reason` 을 남긴다.
    서버는 관측 실패를 해결로 추정하지 않는다.

    Args:
        observation: 해소 여부를 판정한 관측 내용. 어떤 지표를 어떤 구간에서 보았고
                     그 값이 무엇이었는지 쓴다.
        resolved: 관측이 해소를 확인했는지.
        unobservable_reason: 관측으로 확정할 수 없었던 이유.
    """
    if resolved and (not isinstance(observation, str) or not observation.strip()):
        return json.dumps(
            {"ok": False, "error": "resolved=true requires a nonblank observation"},
            ensure_ascii=False,
        )

    ok = _append_record(
        {
            "type": "resolution",
            "observation": redact(observation),
            "resolved": bool(resolved),
            "unobservable_reason": redact(unobservable_reason),
        }
    )
    if not ok:
        return json.dumps({"ok": False, "error": "missing execution context"}, ensure_ascii=False)
    return json.dumps({"ok": True}, ensure_ascii=False)
