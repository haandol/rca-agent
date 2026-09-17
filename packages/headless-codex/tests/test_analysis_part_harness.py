"""Render real profiles locally and inspect capability boundaries without spawning models or tools."""

import tomllib
from pathlib import Path

import pytest

from headless_codex.adapters.secondary.codex import codex_harness as harness


@pytest.mark.parametrize(
    "profile",
    [
        harness.MODEL_EVAL_RECOVERY_PROFILE,
        harness.MODEL_EVAL_ROOT_RCA_PROFILE,
        harness.MODEL_EVAL_ROOT_REPORT_PROFILE,
        harness.MODEL_EVAL_OPERATIONS_PROFILE,
    ],
)
def test_model_eval_profiles_never_expose_live_control_code_or_ci_tools(tmp_path, profile):
    home = tmp_path / profile
    home.mkdir()
    config = tomllib.loads(harness.prepare_codex_home(home, profile).read_text())
    assert config["sandbox_mode"] == "read-only"
    tools = {tool for server in config["mcp_servers"].values() for tool in server["enabled_tools"]}
    assert tools <= {
        "read_analysis_context",
        "save_recovery_result",
        "save_analysis_artifact",
        "save_report_artifact",
        "save_code_proposal",
        "save_operations_result",
    }
    for server in config["mcp_servers"].values():
        assert server["required"] is True
        assert "DYNAMODB_TABLE_NAME" not in server["env_vars"]
        assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in server["env_vars"]


def test_production_root_keeps_readonly_evidence_and_operations_has_associated_ci_reader(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    ops = tmp_path / "ops"
    ops.mkdir()
    config = tomllib.loads(harness.prepare_codex_home(root, harness.ANALYSIS_ROOT_RCA_PROFILE).read_text())
    assert {"cloudwatch", "cloudtrail", "github"} <= config["mcp_servers"].keys()
    assert config["mcp_servers"]["github"]["env"]["GITHUB_READ_ONLY"] == "1"
    assert set(config["mcp_servers"]["github"]["enabled_tools"]) == {
        "get_file_contents",
        "get_commit",
        "list_commits",
        "search_code",
        "pull_request_read",
    }
    config = tomllib.loads(harness.prepare_codex_home(ops, harness.ANALYSIS_OPERATIONS_PROFILE).read_text())
    assert config["mcp_servers"]["analysis-part"]["enabled_tools"] == [
        "read_analysis_context",
        "save_operations_result",
        "read_ci_configuration",
    ]
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in config["mcp_servers"]["analysis-part"]["env_vars"]


def test_recovery_prompt_matches_the_model_choice_without_exposing_server_authority():
    root = Path(__file__).resolve().parents[1]
    guidance = (root / "harness/analysis/agents/recovery-specialist.md").read_text()
    assert "ROLLBACK 또는 UNAVAILABLE" in guidance
    assert "UNAVAILABLE을 선택할 수 있다" in guidance
    assert "playbook, verification, status, identity를 만들지 않는다" in guidance
