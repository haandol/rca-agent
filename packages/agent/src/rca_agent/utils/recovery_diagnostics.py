"""Opt-in recovery diagnostics containing control metadata, never model content."""

import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from rca_agent.utils.exception_logging import safe_exception_info

logger = logging.getLogger(__name__)
_scope: ContextVar[tuple | None] = ContextVar("recovery_diagnostics", default=None)
_FIELDS = frozenset(
    [
        "title",
        "summary",
        "reason",
        "recommendation",
        "playbook",
        "failure_type",
        "symptom_pattern",
        "execution_steps",
        "step_id",
        "intent",
        "action",
        "success_criteria",
        "commands",
        "metric_wait",
        "deployment_wait",
        "ecs_service_precondition",
        "evidence_refs",
        "limitations",
    ]
)
_ERRORS = frozenset(
    [
        "missing",
        "value_error",
        "string_type",
        "string_too_short",
        "literal_error",
        "list_type",
        "dict_type",
        "model_type",
    ]
)


def record(event: str, *, error: BaseException | None = None, **metadata) -> None:
    """Log caller-owned counters/categories inside a recovery scope, excluding exception values."""
    scope = _scope.get()
    if scope is None:
        return
    identifier, started = scope
    payload = {
        "diagnostic_id": identifier,
        "event": event,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        **metadata,
    }
    logger.log(
        logging.WARNING if error else logging.INFO,
        "recovery_diagnostic %s",
        json.dumps(payload, sort_keys=True),
        exc_info=safe_exception_info(error) if error else None,
    )


def validation_errors(error) -> list[dict]:
    """Expose bounded schema paths and error codes without inputs, messages, URLs or custom keys."""
    return [
        {
            "path": [part if type(part) is int or part in _FIELDS else "<field>" for part in item["loc"][:12]],
            "code": item["type"] if item["type"] in _ERRORS else "validation_error",
        }
        for item in error.errors(include_url=False, include_context=False, include_input=False)[:20]
    ]


@contextmanager
def recovery_diagnostics(rca_id: str | None = None):
    """Correlate one real recovery invocation and retain its original result or exception."""
    token = _scope.set((uuid.uuid4().hex, time.monotonic()))
    try:
        canonical_rca_id = str(uuid.UUID(rca_id)) if rca_id else None
    except (ValueError, TypeError, AttributeError):
        canonical_rca_id = None
    record("invocation_started", rca_id=canonical_rca_id)
    try:
        yield
    except BaseException as exc:
        record("invocation_failed", error=exc)
        raise
    else:
        record("invocation_completed")
    finally:
        _scope.reset(token)


@contextmanager
def validation_phase(phase: str):
    """Identify the rejecting server validator while preserving its exception and SDK correction."""
    try:
        yield
    except Exception as exc:
        record("contract_rejected", phase=phase, error=exc)
        raise
