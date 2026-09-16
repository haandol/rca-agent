"""Keep failure types and traceback frames visible without logging model inputs or command values."""

from __future__ import annotations

import json
import re


def safe_exception_info(error: BaseException) -> tuple:
    """Return logging exc_info with original frames and a sanitized diagnostic value.

    Exception messages may embed a whole Pydantic input, command or endpoint query.
    Keep their types, numeric errno and a fixed API operation name instead. The
    wrapper is only for logging; the actual exception and control flow are unchanged.
    """
    chain = []
    current: BaseException | None = error
    seen = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        item = {"type": type(current).__name__}
        number = getattr(current, "errno", None)
        if type(number) is int:
            item["errno"] = number
        operation = getattr(current, "operation_name", None)
        if isinstance(operation, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", operation):
            item["operation"] = operation
        chain.append(item)
        current = current.__cause__ or current.__context__
    safe = RuntimeError(json.dumps({"exception_chain": chain, "exception_values": "omitted"}))
    safe.__suppress_context__ = True
    return type(safe), safe, error.__traceback__
