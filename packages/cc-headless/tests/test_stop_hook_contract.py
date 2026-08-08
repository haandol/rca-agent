import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cc_headless.adapters.secondary.cc.cc_subprocess_runner import _prepare_workspace

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PACKAGE_ROOT / ".claude" / "settings.json"
HOOK_PATH = PACKAGE_ROOT / ".claude" / "hooks" / "validate_stop_artifacts.py"
TOKEN = "a" * 32


def _artifact_dir(tmp_path: Path, token: str = TOKEN) -> Path:
    artifact_dir = tmp_path / "cc-headless-artifacts" / token
    artifact_dir.mkdir(parents=True)
    return artifact_dir


def _run_hook(
    tmp_path: Path,
    *,
    payload: str = '{"stop_hook_active": false}',
    token: str | None = TOKEN,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    if token is None:
        env.pop("RCA_EXECUTION_TOKEN", None)
    else:
        env["RCA_EXECUTION_TOKEN"] = token
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write_complete_rca(artifact_dir: Path) -> None:
    for name in ("scoping.json", "hypotheses.json", "validation-1.json"):
        artifact_dir.joinpath(name).write_text("{}")


def _snapshot(artifact_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in artifact_dir.iterdir()}


def _background_specialist_payload(
    specialist: str,
    *,
    stop_hook_active: bool = False,
) -> str:
    return json.dumps(
        {
            "stop_hook_active": stop_hook_active,
            "background_tasks": [
                {
                    "id": "task-1",
                    "type": "subagent",
                    "status": "running",
                    "description": f"running {specialist}",
                    "agent_type": specialist,
                }
            ],
        }
    )


def test_complete_artifacts_allow_root_stop(tmp_path):
    artifact_dir = _artifact_dir(tmp_path)
    _write_complete_rca(artifact_dir)
    artifact_dir.joinpath("report.md").write_text("# report")
    artifact_dir.joinpath("playbook.json").write_text("{}")
    before = _snapshot(artifact_dir)

    result = _run_hook(tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert _snapshot(artifact_dir) == before


@pytest.mark.parametrize("artifacts_complete", [False, True])
def test_in_flight_report_specialist_waits_for_notification_without_duplicate(
    tmp_path,
    artifacts_complete,
):
    artifact_dir = _artifact_dir(tmp_path)
    _write_complete_rca(artifact_dir)
    if artifacts_complete:
        artifact_dir.joinpath("report.md").write_text("# report")
        artifact_dir.joinpath("playbook.json").write_text("{}")
    before = _snapshot(artifact_dir)

    result = _run_hook(
        tmp_path,
        payload=_background_specialist_payload("report-specialist"),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "Specialist task still in progress (report-specialist)." in result.stderr
    assert "Do not invoke any specialist again." in result.stderr
    assert "wait for its task notification" in result.stderr
    assert "Only an explicit terminal failure permits one retry." in result.stderr
    assert "Retry report-specialist" not in result.stderr
    assert _snapshot(artifact_dir) == before


@pytest.mark.parametrize(
    ("missing_name", "expected"),
    [
        ("scoping.json", "scoping.json"),
        ("hypotheses.json", "hypotheses.json"),
        ("validation-1.json", "validation-{N}.json"),
    ],
)
def test_missing_rca_artifact_requires_terminal_failure_before_retry(
    tmp_path,
    missing_name,
    expected,
):
    artifact_dir = _artifact_dir(tmp_path)
    _write_complete_rca(artifact_dir)
    artifact_dir.joinpath(missing_name).unlink()
    artifact_dir.joinpath("report.md").write_text("# report")
    artifact_dir.joinpath("playbook.json").write_text("{}")
    before = _snapshot(artifact_dir)

    result = _run_hook(tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert expected in result.stderr
    assert "Missing files alone are not a failure." in result.stderr
    assert "Invoke rca-specialist only if it was never started" in result.stderr
    assert "task notification explicitly reports terminal failure" in result.stderr
    assert "do not launch a duplicate" in result.stderr
    assert _snapshot(artifact_dir) == before


@pytest.mark.parametrize("missing_name", ["report.md", "playbook.json"])
def test_missing_report_artifact_requires_terminal_failure_before_retry(
    tmp_path,
    missing_name,
):
    artifact_dir = _artifact_dir(tmp_path)
    _write_complete_rca(artifact_dir)
    for name in {"report.md", "playbook.json"} - {missing_name}:
        artifact_dir.joinpath(name).write_text("{}")
    before = _snapshot(artifact_dir)

    result = _run_hook(tmp_path)

    assert result.returncode == 2
    assert missing_name in result.stderr
    assert "Missing files alone are not a failure." in result.stderr
    assert "Invoke report-specialist only if it was never started" in result.stderr
    assert "task notification explicitly reports terminal failure" in result.stderr
    assert "do not launch a duplicate" in result.stderr
    assert "rca-specialist" not in result.stderr
    assert _snapshot(artifact_dir) == before


def test_active_stop_hook_allows_background_task_to_wake_session_later(tmp_path):
    _artifact_dir(tmp_path)

    result = _run_hook(
        tmp_path,
        payload=_background_specialist_payload(
            "report-specialist",
            stop_hook_active=True,
        ),
    )

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("token", "prepare_context", "payload"),
    [
        (None, False, '{"stop_hook_active": false}'),
        ("not-a-token", False, '{"stop_hook_active": false}'),
        (TOKEN, False, '{"stop_hook_active": false}'),
        (TOKEN, True, "not-json"),
        (TOKEN, True, "{}"),
        (TOKEN, True, '{"stop_hook_active": "false"}'),
        (TOKEN, True, '{"stop_hook_active": false, "background_tasks": {}}'),
    ],
)
def test_invalid_or_missing_run_context_fails_open(
    tmp_path,
    token,
    prepare_context,
    payload,
):
    if prepare_context:
        _artifact_dir(tmp_path)

    result = _run_hook(tmp_path, payload=payload, token=token)

    assert result.returncode == 0
    assert result.stderr == ""


def test_settings_use_official_stop_schema_and_portable_exec_path():
    settings = json.loads(SETTINGS_PATH.read_text())

    assert set(settings["hooks"]) == {"Stop"}
    groups = settings["hooks"]["Stop"]
    assert len(groups) == 1
    assert "matcher" not in groups[0]
    assert len(groups[0]["hooks"]) == 1
    handler = groups[0]["hooks"][0]
    assert handler == {
        "type": "command",
        "command": "python3",
        "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/validate_stop_artifacts.py"],
        "timeout": 5,
    }
    assert "/app/" not in json.dumps(settings)
    assert str(PACKAGE_ROOT) not in json.dumps(settings)


def test_hook_uses_only_python_standard_library():
    imports = set()
    for node in ast.walk(ast.parse(HOOK_PATH.read_text())):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            imports.add(node.module.split(".")[0])

    assert imports == {"json", "os", "pathlib", "re", "sys", "tempfile"}


def test_analysis_workspace_copy_and_image_include_stop_hook(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _prepare_workspace(workspace)

    assert workspace.joinpath(".claude", "settings.json").read_bytes() == SETTINGS_PATH.read_bytes()
    assert workspace.joinpath(".claude", "hooks", HOOK_PATH.name).read_bytes() == HOOK_PATH.read_bytes()
    dockerfile = PACKAGE_ROOT.joinpath("Dockerfile").read_text()
    assert "COPY .claude/ ./.claude/" in dockerfile
