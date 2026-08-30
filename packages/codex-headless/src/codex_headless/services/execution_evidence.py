"""실행 증거의 누적과 요약.

증거는 명령 단위로 남아야 회고가 어느 절차의 무엇을 교정할지 알 수 있다. 실행이
실패해도 보존되며, 자격 증명으로 보이는 인자는 가린다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

_REDACTED = "***REDACTED***"

# 자격 증명으로 보이는 인자 이름. 값이 무엇이든 이름이 이 어휘에 걸리면 가린다 —
# 증거는 사람이 읽는 자료이고 자격 증명이 남으면 열람 자체가 노출이 된다.
# 각 항목은 낱말의 나열이며, 낱말 사이의 구분자(`-`, `_`, `.`, 없음)는 문제 삼지
# 않는다. `--api-key` 와 `apiKey` 와 `API_KEY` 가 같은 것을 가리키기 때문이다.
_SECRET_NAME_PARTS: tuple[tuple[str, ...], ...] = (
    ("secret",),
    ("password",),
    ("passwd",),
    ("token",),
    ("credential",),
    ("api", "key"),
    ("access", "key"),
    ("private", "key"),
    ("auth",),
)
_SECRET_NAME_TOKENS = tuple("".join(parts) for parts in _SECRET_NAME_PARTS)
_SECRET_ALTERNATION = "|".join(r"[-_.]?".join(parts) for parts in _SECRET_NAME_PARTS)

# `--password=...`, `SECRET=...` 형태로 명령 문자열에 직접 실린 값.
_INLINE_SECRET = re.compile(r"(?i)((?:--)?[\w.-]*(?:" + _SECRET_ALTERNATION + r")[\w.-]*\s*[=:]\s*)(\S+)")
# `--password value` 형태. 공백으로 분리된 다음 토큰이 값이다.
_SPACED_SECRET = re.compile(r"(?i)(--[\w.-]*(?:" + _SECRET_ALTERNATION + r")[\w.-]*\s+)(?!-)(\S+)")


class FailureClass(StrEnum):
    """실패의 분류. 회고가 절차 결함과 일시적 오류를 구분하는 기준이다."""

    # 절차의 결함으로 환원되는 실패 — 회고의 교정 대상.
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    MISSING_PRECONDITION = "MISSING_PRECONDITION"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    # 절차의 결함이 아닌 실패 — 재시도로 성공했다면 절차는 옳았다.
    TRANSIENT = "TRANSIENT"
    THROTTLED = "THROTTLED"
    TIMEOUT = "TIMEOUT"
    # 실행 계층이 막은 것 — 절차가 수동 조치로 남는다.
    BLOCKED_DESTRUCTIVE = "BLOCKED_DESTRUCTIVE"
    BLOCKED_UNDECIDABLE = "BLOCKED_UNDECIDABLE"
    UNKNOWN = "UNKNOWN"


# 절차의 결함으로 환원되는 분류. 회고는 이 집합만 교정 입력으로 쓴다.
PROCEDURE_DEFECT_CLASSES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.INVALID_ARGUMENT,
        FailureClass.MISSING_PRECONDITION,
        FailureClass.PERMISSION_DENIED,
        FailureClass.TARGET_NOT_FOUND,
    }
)

BLOCKED_CLASSES: frozenset[FailureClass] = frozenset(
    {FailureClass.BLOCKED_DESTRUCTIVE, FailureClass.BLOCKED_UNDECIDABLE}
)


def parse_failure_class(value: object) -> FailureClass:
    if not isinstance(value, str):
        return FailureClass.UNKNOWN
    try:
        return FailureClass(value.strip().upper())
    except ValueError:
        return FailureClass.UNKNOWN


def redact(text: object) -> str:
    """자격 증명으로 보이는 값을 가린 문자열."""
    if text is None:
        return ""
    rendered = text if isinstance(text, str) else str(text)
    rendered = _INLINE_SECRET.sub(lambda m: f"{m.group(1)}{_REDACTED}", rendered)
    return _SPACED_SECRET.sub(lambda m: f"{m.group(1)}{_REDACTED}", rendered)


def redact_arguments(arguments: object) -> dict[str, str]:
    """인자 맵에서 자격 증명 이름의 값을 가린다."""
    if not isinstance(arguments, dict):
        return {}
    redacted: dict[str, str] = {}
    for raw_name, raw_value in arguments.items():
        name = str(raw_name)
        # 구분자를 지운 뒤 대조한다. `--api-key` 와 `apiKey` 는 같은 것을 가리킨다.
        lowered = re.sub(r"[-_.]", "", name).lower()
        if any(token in lowered for token in _SECRET_NAME_TOKENS):
            redacted[name] = _REDACTED
        else:
            redacted[name] = redact(raw_value)
    return redacted


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class CommandAttempt:
    """한 절차 안의 한 번의 명령 시도."""

    step_id: str
    command: str
    arguments: dict[str, str]
    exit_status: str
    succeeded: bool
    attempt_index: int
    error_output: str = ""
    failure_class: FailureClass | None = None
    blocked: bool = False
    block_reason: str = ""
    observation: str = ""
    recorded_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        payload: dict = {
            "step_id": self.step_id,
            "attempt_index": self.attempt_index,
            "command": self.command,
            "arguments": self.arguments,
            "exit_status": self.exit_status,
            "succeeded": self.succeeded,
            "recorded_at": self.recorded_at,
        }
        if self.error_output:
            payload["error_output"] = self.error_output
        if self.failure_class is not None:
            payload["failure_class"] = str(self.failure_class)
        if self.blocked:
            payload["blocked"] = True
            payload["block_reason"] = self.block_reason
        if self.observation:
            payload["observation"] = self.observation
        return payload


@dataclass
class StepEvidence:
    """한 플레이북 절차의 실행 증거."""

    step_id: str
    intent: str = ""
    success_criteria: str = ""
    attempts: list[CommandAttempt] = field(default_factory=list)
    observation: str = ""
    resolved: bool | None = None
    manual_action_required: bool = False

    @property
    def succeeded(self) -> bool:
        return any(attempt.succeeded for attempt in self.attempts)

    @property
    def blocked(self) -> bool:
        return any(attempt.blocked for attempt in self.attempts)

    @property
    def procedure_defects(self) -> list[CommandAttempt]:
        """절차 결함으로 환원되는 실패만.

        재시도로 성공했더라도 그 실패는 절차가 처음에 틀렸다는 증거이므로 남긴다.
        절차가 옳았는지는 회고가 판단하고, 여기서는 분류만 한다.
        """
        return [attempt for attempt in self.attempts if attempt.failure_class in PROCEDURE_DEFECT_CLASSES]

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "intent": self.intent,
            "success_criteria": self.success_criteria,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "observation": self.observation,
            "succeeded": self.succeeded,
            "blocked": self.blocked,
            "manual_action_required": self.manual_action_required,
        }


@dataclass
class ExecutionEvidence:
    """한 번의 실행 시도가 누적한 증거 전체."""

    execution_id: str
    rca_id: str
    playbook_id: str
    engine: str = ""
    started_at: str = field(default_factory=_now_iso)
    steps: list[StepEvidence] = field(default_factory=list)
    resolution_observation: str = ""
    resolution_confirmed: bool | None = None
    final_state: str = ""
    error_reason: str = ""

    def step(self, step_id: str) -> StepEvidence:
        for existing in self.steps:
            if existing.step_id == step_id:
                return existing
        created = StepEvidence(step_id=step_id)
        self.steps.append(created)
        return created

    def record_attempt(self, attempt: CommandAttempt) -> None:
        self.step(attempt.step_id).attempts.append(attempt)

    @property
    def attempted_step_count(self) -> int:
        return len([step for step in self.steps if step.attempts])

    @property
    def blocked_count(self) -> int:
        return len([step for step in self.steps if step.blocked])

    @property
    def failed_step_count(self) -> int:
        return len([step for step in self.steps if step.attempts and not step.succeeded])

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "rca_id": self.rca_id,
            "playbook_id": self.playbook_id,
            "engine": self.engine,
            "started_at": self.started_at,
            "steps": [step.to_dict() for step in self.steps],
            "resolution_observation": self.resolution_observation,
            "resolution_confirmed": self.resolution_confirmed,
            "final_state": self.final_state,
            "error_reason": self.error_reason,
        }

    def summary(self) -> dict:
        """상태 저장소에 둘 요약. 목록 화면이 오브젝트를 읽지 않아도 되게 한다."""
        return {
            "attempted_step_count": self.attempted_step_count,
            "blocked_count": self.blocked_count,
            "failed_step_count": self.failed_step_count,
            "resolution_confirmed": self.resolution_confirmed,
        }
