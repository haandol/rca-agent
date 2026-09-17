"""Format only existing repository files named by a completed Codex patch."""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def patched_files(event, root):
    """Resolve patch destinations against the event cwd, excluding deleted or external files."""
    if (
        event.get("hook_event_name") != "PostToolUse"
        or event.get("tool_name") != "apply_patch"
    ):
        return []
    patch = event.get("tool_input", {}).get("command", "")
    if not isinstance(patch, str):
        return []
    cwd = Path(event.get("cwd") or root).resolve()
    files = []
    for line in patch.splitlines():
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Move to: "):
            if line.startswith(prefix):
                path = (cwd / line[len(prefix) :]).resolve()
                if path.is_relative_to(root) and path.is_file() and path not in files:
                    files.append(path)
                break
    return files


def format_files(files, root):
    """Use installed project formatters; formatting failure never hides the original edit."""
    prettier = root / "node_modules/.bin/prettier"
    for path in files:
        commands = []
        if path.suffix == ".py":
            ruff = next(
                (
                    parent / ".venv/bin/ruff"
                    for parent in path.parents
                    if parent.is_relative_to(root)
                    and (parent / ".venv/bin/ruff").is_file()
                ),
                shutil.which("ruff"),
            )
            if ruff:
                commands = [
                    [str(ruff), "format", "-q", "--", str(path)],
                    [
                        str(ruff),
                        "check",
                        "-q",
                        "--fix-only",
                        "--select",
                        "I",
                        "--",
                        str(path),
                    ],
                ]
        elif path.suffix in {
            ".ts",
            ".tsx",
            ".vue",
            ".mjs",
            ".cjs",
            ".js",
            ".json",
            ".md",
            ".yml",
            ".yaml",
        }:
            if prettier.is_file():
                commands = [
                    [
                        str(prettier),
                        "--write",
                        "--ignore-unknown",
                        "--log-level",
                        "warn",
                        "--",
                        str(path),
                    ]
                ]
        for command in commands:
            try:
                subprocess.run(
                    command, cwd=root, check=False, stdout=sys.stderr, timeout=25
                )
            except (OSError, subprocess.TimeoutExpired):
                print(
                    f"Formatter unavailable or timed out: {path.name}", file=sys.stderr
                )


def main():
    """Consume the documented Codex event without requiring a developer-specific environment variable."""
    try:
        event = json.load(sys.stdin)
        root = Path(__file__).resolve().parents[2]
        format_files(patched_files(event, root), root)
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        print(f"Patch formatting skipped: {type(exc).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()
