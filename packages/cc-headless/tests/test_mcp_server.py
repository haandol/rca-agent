import json
import os
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from cc_headless import mcp_server
from cc_headless.services import execution_context
from cc_headless.services.execution_context import RUN_TOKEN_ENV, ExecutionContext


@pytest.fixture
def artifact_home(monkeypatch, tmp_path):
    token = uuid.uuid4().hex
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setenv(RUN_TOKEN_ENV, token)
    context = ExecutionContext(rca_id="rca-1", token=token)
    base = context.prepare()
    yield base
    context.cleanup()


def _save_rejected(filename: str, content: str) -> bool:
    try:
        result = json.loads(mcp_server.save_artifact(filename, content))
    except (OSError, ValueError):
        return True
    return result.get("ok") is False


@pytest.mark.parametrize(
    "filename",
    [
        "../escaped.json",
        "../../escaped.json",
        "/tmp/escaped.json",
        "nested/report.md",
        r"..\escaped.json",
    ],
)
def test_save_artifact_rejects_path_traversal_and_nested_paths(artifact_home, filename):
    escaped = artifact_home.parent / "escaped.json"
    escaped.unlink(missing_ok=True)

    try:
        assert _save_rejected(filename, "{}") is True
        assert not escaped.exists()
    finally:
        escaped.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "filename",
    ["scoping.json", "hypotheses.json", "validation-1.json", "validation-99.json", "playbook.json", "report.md"],
)
def test_save_artifact_accepts_canonical_names(artifact_home, filename):
    content = "# report" if filename == "report.md" else "{}"

    result = json.loads(mcp_server.save_artifact(filename, content))

    assert result["ok"] is True
    assert Path(result["path"]) == artifact_home / filename
    assert (artifact_home / filename).read_text() == content


@pytest.mark.parametrize(
    "filename",
    ["notes.txt", "validation-1.md", "validation-x.json", "report.json", "scoping.md", ".hidden.json"],
)
def test_save_artifact_rejects_unknown_names_and_extensions(artifact_home, filename):
    assert _save_rejected(filename, "content") is True
    assert not (artifact_home / filename).exists()


def test_save_artifact_preserves_existing_file_when_atomic_replace_fails(artifact_home, monkeypatch):
    target = artifact_home / "report.md"
    target.write_text("stable report")

    def _replace_failure(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _replace_failure)
    with suppress(OSError):
        mcp_server.save_artifact("report.md", "new report")

    assert target.read_text() == "stable report"


@pytest.mark.parametrize("token", [None, "", "../escape", "g" * 32, "a" * 31, "a" * 33])
def test_save_artifact_rejects_missing_or_invalid_execution_token(monkeypatch, tmp_path, token):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    if token is None:
        monkeypatch.delenv(RUN_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(RUN_TOKEN_ENV, token)

    result = json.loads(mcp_server.save_artifact("report.md", "must not be written"))

    assert result["ok"] is False
    assert not (tmp_path / "runs").exists()


def test_save_artifact_rejects_valid_token_without_prepared_run_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setenv(RUN_TOKEN_ENV, uuid.uuid4().hex)

    result = json.loads(mcp_server.save_artifact("report.md", "must not be written"))

    assert result["ok"] is False


def test_save_artifact_rejects_symlinked_run_directory(monkeypatch, tmp_path):
    token = uuid.uuid4().hex
    artifact_root = tmp_path / "runs"
    outside = tmp_path / "outside"
    artifact_root.mkdir()
    outside.mkdir()
    artifact_root.joinpath(token).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", artifact_root)
    monkeypatch.setenv(RUN_TOKEN_ENV, token)

    result = json.loads(mcp_server.save_artifact("report.md", "must not be written"))

    assert result["ok"] is False
    assert not outside.joinpath("report.md").exists()
