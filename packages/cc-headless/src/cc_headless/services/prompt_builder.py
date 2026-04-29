from __future__ import annotations

import re
from pathlib import Path

from cc_headless.ports.dto.models import AlarmContext


def _find_prompts_dir() -> Path:
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        candidate = parent / "prompts"
        if candidate.is_dir():
            return candidate
    return Path("/app/prompts")


_PROMPTS_DIR = _find_prompts_dir()
_INCLUDE_PATTERN = re.compile(r"\{\{\s*include:\s*([^}\s]+\.md)\s*\}\}")
_MAX_INCLUDE_DEPTH = 8


def _resolve_includes(text: str, base_dir: Path, depth: int = 0) -> str:
    if depth >= _MAX_INCLUDE_DEPTH:
        raise RuntimeError(f"Prompt include depth exceeded {_MAX_INCLUDE_DEPTH}")

    def _replace(match: re.Match[str]) -> str:
        relative = match.group(1).strip()
        target = (base_dir / relative).resolve()
        prompts_root = _PROMPTS_DIR.resolve()
        if prompts_root not in target.parents and target != prompts_root:
            raise RuntimeError(f"Include target outside prompts dir: {relative}")
        content = target.read_text()
        return _resolve_includes(content, target.parent, depth + 1)

    return _INCLUDE_PATTERN.sub(_replace, text)


def build_prompt(alarm: AlarmContext) -> str:
    system_raw = (_PROMPTS_DIR / "rca-system.md").read_text()
    system_prompt = _resolve_includes(system_raw, _PROMPTS_DIR)
    user_template = (_PROMPTS_DIR / "rca-user.md").read_text()

    dimensions_str = ", ".join(f"{k}={v}" for k, v in alarm.dimensions.items()) if alarm.dimensions else "N/A"

    replacements = {
        "{alarm_name}": alarm.alarm_name,
        "{state_reason}": alarm.state_reason,
        "{state_change_time}": alarm.state_change_time or "N/A",
        "{region}": alarm.region,
        "{namespace}": alarm.namespace or "N/A",
        "{metric_name}": alarm.metric_name or "N/A",
        "{dimensions}": dimensions_str,
        "{statistic}": alarm.statistic or "Average",
        "{period}": str(alarm.period or 300),
        "{threshold}": str(alarm.threshold) if alarm.threshold is not None else "N/A",
        "{comparison_operator}": alarm.comparison_operator or "N/A",
    }

    user_prompt = user_template
    for placeholder, value in replacements.items():
        user_prompt = user_prompt.replace(placeholder, value)

    return f"{system_prompt}\n\n---\n\n{user_prompt}"
