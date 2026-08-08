from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
_VALIDATION_PATTERN = re.compile(r"validation-[1-9][0-9]*\.json")
_RCA_ARTIFACTS = ("scoping.json", "hypotheses.json")
_REPORT_ARTIFACTS = ("report.md", "playbook.json")
_SPECIALISTS = frozenset(("rca-specialist", "report-specialist"))


def _read_hook_input() -> dict | None:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None
    if not isinstance(value, dict):
        return None
    stop_hook_active = value.get("stop_hook_active")
    background_tasks = value.get("background_tasks", [])
    if not isinstance(stop_hook_active, bool) or not isinstance(background_tasks, list):
        return None
    return value


def _is_artifact(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _missing_rca_artifacts(artifact_dir: Path) -> list[str]:
    missing = [name for name in _RCA_ARTIFACTS if not _is_artifact(artifact_dir / name)]
    has_validation = any(
        _VALIDATION_PATTERN.fullmatch(path.name) and _is_artifact(path) for path in artifact_dir.iterdir()
    )
    if not has_validation:
        missing.append("validation-{N}.json")
    return missing


def _in_flight_specialists(hook_input: dict) -> list[str]:
    specialists = {
        task.get("agent_type")
        for task in hook_input.get("background_tasks", [])
        if isinstance(task, dict) and task.get("type") == "subagent" and task.get("agent_type") in _SPECIALISTS
    }
    return sorted(specialists)


def _block_for_in_flight_specialists(specialists: list[str]) -> int:
    names = ", ".join(specialists)
    print(
        f"Specialist task still in progress ({names}). Do not invoke any specialist again. "
        "End this turn and wait for its task notification. "
        "Only an explicit terminal failure permits one retry.",
        file=sys.stderr,
    )
    return 2


def _block_for_missing_artifacts(specialist: str, missing: list[str]) -> int:
    names = ", ".join(missing)
    print(
        f"Required artifacts missing ({names}). Missing files alone are not a failure. "
        f"Invoke {specialist} only if it was never started; retry it once only after its prior "
        "Agent result or task notification explicitly reports terminal failure. "
        "Otherwise wait for that notification and do not launch a duplicate.",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    hook_input = _read_hook_input()
    if hook_input is None or hook_input["stop_hook_active"]:
        return 0

    token = os.environ.get("RCA_EXECUTION_TOKEN", "")
    if _TOKEN_PATTERN.fullmatch(token) is None:
        return 0

    artifact_dir = Path(tempfile.gettempdir()) / "cc-headless-artifacts" / token
    if not artifact_dir.is_dir() or artifact_dir.is_symlink():
        return 0

    try:
        in_flight_specialists = _in_flight_specialists(hook_input)
        if in_flight_specialists:
            return _block_for_in_flight_specialists(in_flight_specialists)

        missing_rca = _missing_rca_artifacts(artifact_dir)
        if missing_rca:
            return _block_for_missing_artifacts("rca-specialist", missing_rca)

        missing_report = [name for name in _REPORT_ARTIFACTS if not _is_artifact(artifact_dir / name)]
        if missing_report:
            return _block_for_missing_artifacts("report-specialist", missing_report)
    except OSError:
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
