import json
import os
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from cc_headless import mcp_server
from cc_headless.services import artifact_validation, execution_context
from cc_headless.services.execution_context import (
    RUN_TOKEN_ENV,
    ExecutionContext,
)

_EVIDENCE_WINDOWS = (
    "- Current alarm window: 2026-07-29T13:00:00Z ~ 2026-07-29T14:00:00Z\n"
    "- Historical comparison window: 2026-07-29T12:00:00Z ~ 2026-07-29T13:00:00Z\n"
)


def _minimal_valid_artifact(filename: str) -> str:
    """Smallest content that passes the save-time shape check for each artifact."""
    if filename == "report.md":
        return "\n".join(
            f"## {title}\n{_EVIDENCE_WINDOWS if title == '증거 시간 범위' else 'placeholder'}\n"
            for title in artifact_validation._REPORT_SECTIONS
        )

    if filename == "scoping.json":
        return json.dumps(
            {
                "stage": "SCOPING",
                "alarm_name": "alarm",
                "impact_scope": "service",
                "severity": "high",
                "summary": "s",
                "output_summary": "o",
                "metric_observations": [
                    {
                        "metric_name": "DatabaseConnections",
                        "datapoints": [2, 12, 20, 27, 30],
                        "trend": "rising",
                    }
                ],
                "concurrent_alarms": [],
            }
        )

    if filename == "hypotheses.json":
        return json.dumps(
            {
                "stage": "HYPOTHESIS_GENERATION",
                "tree_id": "tree-1",
                "summary": "s",
                "output_summary": "o",
                "hypotheses": [{"hypothesis_id": "h1"}],
            }
        )

    if filename == "playbook.json":
        artifact: dict = {field: "value" for field in artifact_validation._PLAYBOOK_STRING_FIELDS}
        artifact["stage"] = "PLAYBOOK"
        artifact["verification_status"] = "DRAFT"
        artifact.update({field: [] for field in artifact_validation._PLAYBOOK_LIST_FIELDS})
        return json.dumps(artifact)

    return "{}"


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
    content = _minimal_valid_artifact(filename)

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


@pytest.mark.parametrize("missing", artifact_validation._PLAYBOOK_STRING_FIELDS)
def test_save_artifact_rejects_playbook_missing_a_required_field(artifact_home, missing):
    # A field omitted here used to be accepted and only surfaced at the
    # completion gate, after the run had ended and could no longer be corrected.
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    del artifact[missing]

    result = json.loads(mcp_server.save_artifact("playbook.json", json.dumps(artifact)))

    assert result["ok"] is False
    assert missing in result["error"]
    assert not (artifact_home / "playbook.json").exists()


def test_save_artifact_rejection_tells_the_agent_to_save_again(artifact_home):
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    del artifact["severity_criteria"]

    result = json.loads(mcp_server.save_artifact("playbook.json", json.dumps(artifact)))

    assert "save again" in result["error"]


@pytest.mark.parametrize(
    "filename",
    ["scoping.json", "hypotheses.json", "playbook.json"],
)
def test_save_artifact_rejects_malformed_json(artifact_home, filename):
    assert _save_rejected(filename, "{not json") is True
    assert not (artifact_home / filename).exists()


@pytest.mark.parametrize(
    "filename",
    ["scoping.json", "hypotheses.json", "playbook.json"],
)
def test_save_artifact_rejects_a_json_array(artifact_home, filename):
    assert _save_rejected(filename, "[]") is True
    assert not (artifact_home / filename).exists()


def test_save_artifact_rejects_report_missing_a_required_section(artifact_home):
    full = _minimal_valid_artifact("report.md")
    truncated = full.split("## Action Items")[0]

    assert _save_rejected("report.md", truncated) is True
    assert not (artifact_home / "report.md").exists()


def test_save_artifact_rejects_wrong_stage_value(artifact_home):
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    artifact["stage"] = "REPORT"

    assert _save_rejected("playbook.json", json.dumps(artifact)) is True


def test_save_artifact_still_accepts_validation_loops_without_shape_rules(artifact_home):
    # Validation artifacts are checked against hypotheses the save call cannot
    # see, so their shape stays with the completion gate.
    result = json.loads(mcp_server.save_artifact("validation-1.json", "{}"))

    assert result["ok"] is True


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


def test_analysis_server_exposes_no_tool_that_changes_a_service():
    # The analysis run is read-only. Recovery happens in a separate agent after a
    # person approves, so a write tool here would put execution back inside
    # analysis and bypass that approval.
    tools = [name for name in dir(mcp_server) if not name.startswith("_")]

    assert "execute_healthcare_reset" not in tools
    assert not any("reset" in name.lower() for name in tools)


def test_save_artifact_rejects_a_server_owned_remediation_result(artifact_home):
    # remediation.json belonged to the retired automated-recovery path. Nothing
    # writes it now, and the analysis agent must not resurrect it to claim a
    # recovery it never performed.
    assert _save_rejected("remediation.json", "{}") is True
    assert not (artifact_home / "remediation.json").exists()


def test_save_artifact_rejects_a_playbook_claiming_it_was_verified(artifact_home):
    # A playbook is a draft until an execution and its retrospective exercise it,
    # so analysis may not present one as verified.
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    artifact["verification_status"] = "VERIFIED"

    result = json.loads(mcp_server.save_artifact("playbook.json", json.dumps(artifact)))

    assert result["ok"] is False
    assert "DRAFT" in result["error"]


def test_save_artifact_rejects_execution_steps_missing_their_contract(artifact_home):
    for missing in artifact_validation._EXECUTION_STEP_FIELDS:
        artifact = json.loads(_minimal_valid_artifact("playbook.json"))
        step = {field: "value" for field in artifact_validation._EXECUTION_STEP_FIELDS}
        del step[missing]
        artifact["execution_steps"] = [step]

        result = json.loads(mcp_server.save_artifact("playbook.json", json.dumps(artifact)))

        assert result["ok"] is False, missing
        assert missing in result["error"]


def test_save_artifact_rejects_duplicate_execution_step_ids(artifact_home):
    # Evidence and the retrospective address a step by its ID, so a duplicate
    # would make the failing step ambiguous.
    artifact = json.loads(_minimal_valid_artifact("playbook.json"))
    step = {field: "value" for field in artifact_validation._EXECUTION_STEP_FIELDS}
    artifact["execution_steps"] = [dict(step), dict(step)]

    result = json.loads(mcp_server.save_artifact("playbook.json", json.dumps(artifact)))

    assert result["ok"] is False
    assert "unique" in result["error"]
