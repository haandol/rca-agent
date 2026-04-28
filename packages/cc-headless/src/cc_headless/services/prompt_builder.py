from __future__ import annotations

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


def build_prompt(alarm: AlarmContext) -> str:
    system_prompt = (_PROMPTS_DIR / "rca-system.md").read_text()
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
