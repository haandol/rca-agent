import uuid

import pytest

from cc_headless.services import execution_context
from cc_headless.services.execution_context import ExecutionContext, artifact_dir_for_token


def test_same_rca_id_gets_a_distinct_artifact_directory_for_every_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")

    first = ExecutionContext.create("same-rca-id")
    second = ExecutionContext.create("same-rca-id")

    assert first.rca_id == second.rca_id == "same-rca-id"
    assert first.token != second.token
    assert first.prepare() != second.prepare()


@pytest.mark.parametrize("token", ["", "../escape", "/absolute", "a-b", "g" * 32, "a" * 31, "a" * 33])
def test_artifact_path_rejects_unsafe_execution_tokens(token):
    with pytest.raises(ValueError, match="invalid RCA execution token"):
        artifact_dir_for_token(token)


def test_cleanup_removes_only_the_current_execution_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "runs")
    current = ExecutionContext(rca_id="rca-1", token=uuid.uuid4().hex)
    other = ExecutionContext(rca_id="rca-1", token=uuid.uuid4().hex)
    current.prepare().joinpath("report.md").write_text("current")
    other.prepare().joinpath("report.md").write_text("other")
    legacy = tmp_path / "rca-rca-1" / "report.md"
    legacy.parent.mkdir()
    legacy.write_text("legacy")

    current.cleanup()

    assert not current.artifact_dir.exists()
    assert other.artifact_dir.joinpath("report.md").read_text() == "other"
    assert legacy.read_text() == "legacy"
