from __future__ import annotations

EMBED_FIELD_MAX = 80


def build_embed_key(*, failure_type: str = "", symptom: str = "", metric_name: str = "") -> str:
    """Render the structured embedding text shared by every vector index.

    Writers and searchers must produce identical text for the same incident, so
    both go through this one renderer: fields are truncated the same way and an
    empty field drops its segment rather than leaving a dangling label behind.
    """
    parts = {
        "장애유형": _truncate(failure_type),
        "증상": _truncate(symptom),
        "메트릭": _truncate(metric_name),
    }
    return " | ".join(f"{label}: {value}" for label, value in parts.items() if value)


def _truncate(text: str) -> str:
    return text[:EMBED_FIELD_MAX].strip() if text else ""
