"""실행 증거의 누적과 요약.

증거는 명령 단위로 남아야 회고가 어느 절차의 무엇을 교정할지 알 수 있다. 실행이
실패해도 보존되며, 자격 증명으로 보이는 인자는 가린다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

_REDACTED = "***REDACTED***"
MAX_OUTPUT_CHARS = 20_000

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
_PUBLIC_NAMES = frozenset(
    {
        "nexttoken",
        "startingtoken",
        "continuationtoken",
        "paginationtoken",
        "clienttoken",
        "requestid",
        "taskid",
        "taskarn",
        "secretarn",
        "secretid",
        "credentialid",
        "author",
        "authorid",
        "authorizerid",
        "authorizerarn",
        "tokenid",
        "tokencount",
    }
)
_QUOTED_VALUE = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
_VALUE = rf"""(?:{_QUOTED_VALUE}|[^\s,;}}\]"']+)"""
_INLINE_SECRET = re.compile(rf"""(?<![\w.-])(?P<prefix>(?P<name>[\w.-]+)["']?\s*[=:]\s*)(?P<value>{_VALUE})""")
_SPACED_SECRET = re.compile(rf"(?P<prefix>--(?P<name>[\w.-]+)\s+)(?P<value>{_VALUE})")
_BEARER = re.compile(r"""(?i)(\bBearer\s+)[^\s"'\\,;}\]]+""")
_URL_USERINFO = re.compile(r"""(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s"'<>@]+@""")
# ECS environment entries can occur inside mixed diagnostic text, in either key order.
_ENV_PAIR = re.compile(
    rf"""(?i)(["']?(?:name|key)["']?\s*[:=]\s*["']?(?P<name>[\w.-]+)["']?\s*,\s*"""
    rf"""["']?value["']?\s*[:=]\s*)(?P<value>{_VALUE})"""
)
_ENV_PAIR_REVERSED = re.compile(
    rf"""(?i)(["']?value["']?\s*[:=]\s*)(?P<value>{_VALUE})"""
    r"""(?P<suffix>\s*,\s*["']?(?:name|key)["']?\s*[:=]\s*["']?(?P<name>[\w.-]+)["']?)"""
)


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


def _secret_name(name: str) -> bool:
    """Recognize credential field names while retaining known pagination tokens and resource IDs."""
    normalized = re.sub(r"[-_.]", "", name).lower()
    return normalized not in _PUBLIC_NAMES and any(token in normalized for token in _SECRET_NAME_TOKENS)


def _redacted_value(value: str) -> str:
    """Replace a matched secret value, preserving surrounding JSON or shell quotes."""
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[0] + _REDACTED + value[-1]
    return _REDACTED


def _redact_match(match: re.Match) -> str:
    """Redact a named assignment only when its field denotes a credential."""
    if not _secret_name(match.group("name")):
        return match.group(0)
    return match.group(1) + _redacted_value(match.group("value")) + (match.groupdict().get("suffix") or "")


def _redact_json(value: object) -> object:
    """Redact structured JSON recursively, including name/value environment entries in any key order."""
    if isinstance(value, dict):
        env_name = value.get("name", value.get("key"))
        secret_env = isinstance(env_name, str) and _secret_name(env_name)
        return {
            key: _REDACTED if _secret_name(key) or (key == "value" and secret_env) else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def redact(text: object) -> str:
    """Redact credential values before retention; preserve benign IDs and valid structured JSON."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    rendered = text if isinstance(text, str) else str(text)
    if rendered.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(rendered)
        except (ValueError, RecursionError):
            pass
        else:
            cleaned = _redact_json(parsed)
            if cleaned != parsed:
                rendered = json.dumps(cleaned, ensure_ascii=False)
    rendered = _BEARER.sub(lambda m: m.group(1) + _REDACTED, rendered)
    rendered = _ENV_PAIR.sub(_redact_match, rendered)
    rendered = _ENV_PAIR_REVERSED.sub(_redact_match, rendered)
    rendered = _INLINE_SECRET.sub(_redact_match, rendered)
    rendered = _SPACED_SECRET.sub(_redact_match, rendered)
    return _URL_USERINFO.sub(lambda m: m.group(1) + _REDACTED + "@", rendered)


def redact_arguments(arguments: object) -> dict[str, str]:
    """인자 맵에서 자격 증명 이름의 값을 가린다."""
    if not isinstance(arguments, dict):
        return {}
    redacted: dict[str, str] = {}
    for raw_name, raw_value in arguments.items():
        name = str(raw_name)
        # 구분자를 지운 뒤 대조한다. `--api-key` 와 `apiKey` 는 같은 것을 가리킨다.
        if _secret_name(name):
            redacted[name] = _REDACTED
        else:
            redacted[name] = redact(raw_value)
    return redacted


def capture_command_output(stdout: object, stderr: object) -> dict:
    """Capture redacted stream prefixes at the existing cap, with counts in redacted characters.

    Counts describe the text after redaction, not bytes received or secret lengths. A truncated
    stream is a preview and must not be interpreted as complete JSON or complete command output.
    """
    captured: dict = {}
    for name, value in (("stdout", stdout), ("stderr", stderr)):
        safe = redact(value)
        retained = safe[:MAX_OUTPUT_CHARS]
        captured.update(
            {
                name: retained,
                f"{name}_chars": len(safe),
                f"{name}_retained_chars": len(retained),
                f"{name}_omitted_chars": len(safe) - len(retained),
                f"{name}_truncated": len(safe) > len(retained),
            }
        )
    return captured


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
    recorded_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    intent: str = ""
    captured_output: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize retained command evidence; never manufacture missing historical timestamps."""
        payload: dict = {
            "step_id": self.step_id,
            "attempt_index": self.attempt_index,
            "command": self.command,
            "arguments": self.arguments,
            "exit_status": self.exit_status,
            "succeeded": self.succeeded,
            "intent": self.intent,
        }
        for name in ("started_at", "ended_at", "recorded_at"):
            if (moment := getattr(self, name)) is not None:
                payload[name] = moment
        payload.update(self.captured_output)
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
    outcomes: list[dict] = field(default_factory=list)

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
        """Serialize step identity, attempts and all accepted outcome records without changing verdicts."""
        return {
            "step_id": self.step_id,
            "intent": self.intent,
            "success_criteria": self.success_criteria,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "observation": self.observation,
            "succeeded": self.succeeded,
            "blocked": self.blocked,
            "manual_action_required": self.manual_action_required,
            "resolved": self.resolved,
            "outcomes": self.outcomes,
        }


@dataclass
class ExecutionEvidence:
    """한 번의 실행 시도가 누적한 증거 전체."""

    execution_id: str
    rca_id: str
    playbook_id: str
    engine: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    steps: list[StepEvidence] = field(default_factory=list)
    resolution_observation: str = ""
    resolution_confirmed: bool | None = None
    final_state: str = ""
    error_reason: str = ""
    resolution_records: list[dict] = field(default_factory=list)
    metric_wait_records: list[dict] = field(default_factory=list)

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
        """Serialize durable evidence with observed execution times only; legacy times remain absent."""
        payload = {
            "execution_id": self.execution_id,
            "rca_id": self.rca_id,
            "playbook_id": self.playbook_id,
            "engine": self.engine,
            "steps": [step.to_dict() for step in self.steps],
            "resolution_observation": self.resolution_observation,
            "resolution_confirmed": self.resolution_confirmed,
            "final_state": self.final_state,
            "error_reason": self.error_reason,
            "resolution_records": self.resolution_records,
        }
        for name in ("started_at", "ended_at"):
            if (moment := getattr(self, name)) is not None:
                payload[name] = moment
        if self.metric_wait_records:
            payload["metric_wait_records"] = self.metric_wait_records
        return payload

    def summary(self) -> dict:
        """상태 저장소에 둘 요약. 목록 화면이 오브젝트를 읽지 않아도 되게 한다."""
        return {
            "attempted_step_count": self.attempted_step_count,
            "blocked_count": self.blocked_count,
            "failed_step_count": self.failed_step_count,
            "resolution_confirmed": self.resolution_confirmed,
        }


def retrospective_evidence_json(evidence: ExecutionEvidence, *, max_chars: int = 60_000) -> str:
    """Return valid bounded JSON without mutating durable evidence or omitting steps and verdicts.

    Only per-attempt output/observation/error previews shrink. Identity, command metadata,
    outcome records, resolution and timestamps remain intact. Explicit per-field omissions
    distinguish this projection from persisted evidence. If metadata alone cannot fit, raise
    ValueError so the existing retrospective failure path applies instead of supplying bad JSON.
    """
    payload = evidence.to_dict()
    # Round-trip makes nested outcome records independent of the durable evidence object.
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    previews = [
        (attempt, name, attempt[name])
        for step in payload["steps"]
        for attempt in step["attempts"]
        for name in ("stdout", "stderr", "observation", "error_output")
        if isinstance(attempt.get(name), str) and attempt[name]
    ]
    # Fixed-wait receipts retain a second audit copy of query output. Apply the
    # same preview budget to that copy; the durable S3 journal remains intact.
    previews.extend(
        (record["response"], name, record["response"][name])
        for record in payload.get("metric_wait_records", [])
        if isinstance(record.get("response"), dict)
        for name in ("stdout", "stderr")
        if isinstance(record["response"].get(name), str) and record["response"][name]
    )
    payload["projection"] = {
        "output_previews_omitted": False,
        "omitted_chars": 0,
        "persisted_evidence_unchanged": True,
    }

    def render(limit: int) -> str:
        """Serialize a uniform output preview budget, counting JSON escaping in the final size."""
        omitted = 0
        for attempt, name, original in previews:
            attempt[name] = original[:limit]
            count = len(original) - len(attempt[name])
            attempt.setdefault("projection_omissions", {})[name] = {
                "omitted": bool(count),
                "omitted_chars": count,
            }
            omitted += count
        payload["projection"]["output_previews_omitted"] = bool(omitted)
        payload["projection"]["omitted_chars"] = omitted
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    upper = max((len(original) for _, _, original in previews), default=0)
    full = render(upper)
    if len(full) <= max_chars:
        return full
    minimum = render(0)
    if len(minimum) > max_chars:
        raise ValueError("retrospective evidence metadata exceeds JSON budget; durable evidence remains available")
    lower = 0
    best = minimum
    while lower < upper:
        middle = (lower + upper + 1) // 2
        candidate = render(middle)
        if len(candidate) <= max_chars:
            lower, best = middle, candidate
        else:
            upper = middle - 1
    return best
