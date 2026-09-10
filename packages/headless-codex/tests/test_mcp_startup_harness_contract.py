"""Offline contracts for the generated configs used by the sequential CLI workers."""

import tomllib

import pytest

from headless_codex.adapters.secondary.codex.codex_harness import (
    ANALYSIS_RCA_PROFILE,
    ANALYSIS_REPORT_PROFILE,
    EXECUTION_PROFILE,
    MODEL_EVAL_RCA_PROFILE,
    MODEL_EVAL_REPORT_PROFILE,
    RETROSPECTIVE_PROFILE,
    codex_environment,
    prepare_codex_home,
)

REQUIRED_SERVERS = {
    ANALYSIS_RCA_PROFILE: {"cloudwatch", "cloudtrail", "rca-progress"},
    ANALYSIS_REPORT_PROFILE: {"rca-progress"},
    MODEL_EVAL_RCA_PROFILE: {"rca-progress"},
    MODEL_EVAL_REPORT_PROFILE: {"rca-progress"},
    EXECUTION_PROFILE: {"cloudwatch", "playbook-execution"},
    RETROSPECTIVE_PROFILE: {"playbook-retrospective"},
}
UV_ENV_NAMES = {"UV_CACHE_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_OFFLINE"}
AWS_ENV_NAMES = {
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
}


def _render(tmp_path, profile):
    """Read the actual rendered runtime config, not a parallel test-only template."""
    home = tmp_path / profile
    home.mkdir()
    path = prepare_codex_home(home, profile)
    return home, tomllib.loads(path.read_text())


@pytest.mark.parametrize("profile", REQUIRED_SERVERS)
def test_initial_catalog_waits_for_servers_and_essential_startup_failures_are_fatal(tmp_path, profile):
    """Codex 0.151 required=true waits for initialization and fails on startup errors.

    Pinned upstream proof: codex-rs/exec/tests/suite/mcp_required_exit.rs.
    Optional AWS Knowledge/GitHub startup failures must not abort domain analysis.
    """
    _, config = _render(tmp_path, profile)
    servers = config["mcp_servers"]
    optional = {"aws-knowledge", "github"} if profile == ANALYSIS_RCA_PROFILE else set()
    assert set(servers) == REQUIRED_SERVERS[profile] | optional
    assert {name for name, server in servers.items() if server.get("required")} == REQUIRED_SERVERS[profile]
    for name in optional:
        assert servers[name]["required"] is False
    # The first model request must not race a cold MCP startup's initial tool listing.
    assert config["mcp_optional_startup_grace_ms"] == 0
    assert config["approval_policy"] == "never"
    assert config["sandbox_mode"] == "read-only"
    for name, server in servers.items():
        assert server["startup_timeout_sec"] == 30
        assert server["tool_timeout_sec"] == (360 if name == "playbook-execution" else 120)
        assert server["default_tools_approval_mode"] == "approve"


@pytest.mark.parametrize(
    ("profile", "server_name"),
    [
        (ANALYSIS_RCA_PROFILE, "cloudwatch"),
        (ANALYSIS_RCA_PROFILE, "cloudtrail"),
        (EXECUTION_PROFILE, "cloudwatch"),
    ],
)
def test_uvx_receives_existing_image_cache_settings_without_new_credentials(
    tmp_path, monkeypatch, profile, server_name
):
    """An isolated CODEX_HOME still uses the installed uv tool/cache locations and offline mode."""
    inherited_uv = {
        "UV_CACHE_DIR": "/installed/cache",
        "UV_TOOL_DIR": "/installed/tools",
        "UV_TOOL_BIN_DIR": "/installed/tools/bin",
        "UV_OFFLINE": "true",
    }
    for name, value in inherited_uv.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("UV_INDEX_PASSWORD", "must-not-forward")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-add")
    home, config = _render(tmp_path, profile)
    server = config["mcp_servers"][server_name]
    assert server["command"] == "uvx"
    names = server["env_vars"]
    assert len(names) == len(set(names))
    assert set(names) == AWS_ENV_NAMES | UV_ENV_NAMES
    assert UV_ENV_NAMES.isdisjoint(server["env"])  # No hardcoded values overriding the inherited ones.
    environment = codex_environment(home, {})
    assert {name: environment[name] for name in names if name in UV_ENV_NAMES} == inherited_uv
    assert environment["HOME"] == str(home)

    # Bare local execution must not acquire fabricated image paths or offline settings.
    for name in UV_ENV_NAMES:
        monkeypatch.delenv(name)
    assert UV_ENV_NAMES.isdisjoint(codex_environment(home, {}))


@pytest.mark.parametrize(
    ("profile", "tool"),
    [
        (MODEL_EVAL_RCA_PROFILE, "save_analysis_artifact"),
        (MODEL_EVAL_REPORT_PROFILE, "save_report_artifact"),
    ],
)
def test_model_eval_requires_artifact_startup_without_acquiring_aws_or_uv_environment(tmp_path, profile, tool):
    """Local-observation evaluation waits for its saving tool without gaining live evidence access."""
    _, config = _render(tmp_path, profile)
    server = config["mcp_servers"]["rca-progress"]
    assert server["required"] is True
    assert server["enabled_tools"] == [tool]
    assert set(server["env_vars"]) == {"RCA_EXECUTION_TOKEN", "RCA_SESSION_ID", "RCA_CLAIM_TOKEN", "RCA_ATTEMPT"}
    assert (AWS_ENV_NAMES | UV_ENV_NAMES).isdisjoint(server["env"])


def test_execution_mcp_forwards_the_existing_aws_cli_environment_names(tmp_path):
    """AWS CLI inherits the MCP process environment, so its AWS allowlist must match CloudWatch."""
    _, config = _render(tmp_path, EXECUTION_PROFILE)
    execution = config["mcp_servers"]["playbook-execution"]
    cloudwatch = config["mcp_servers"]["cloudwatch"]
    playbook_names = {
        "PLAYBOOK_EXECUTION_TOKEN",
        "PLAYBOOK_EXECUTION_ID",
        "PLAYBOOK_APPROVED_STEP_IDS",
        "PLAYBOOK_APPROVED_SUCCESS_CRITERIA",
    }
    names = execution["env_vars"]
    assert len(names) == len(set(names))
    assert set(cloudwatch["env_vars"]) - UV_ENV_NAMES == AWS_ENV_NAMES
    assert set(names) == playbook_names | AWS_ENV_NAMES
    assert (AWS_ENV_NAMES | UV_ENV_NAMES).isdisjoint(execution["env"])


def test_retrospective_artifact_server_does_not_acquire_aws_environment(tmp_path):
    """Retrospective only saves local artifacts and needs no AWS credential discovery variables."""
    _, config = _render(tmp_path, RETROSPECTIVE_PROFILE)
    server = config["mcp_servers"]["playbook-retrospective"]
    assert set(server["env_vars"]) == {"PLAYBOOK_EXECUTION_TOKEN", "PLAYBOOK_EXECUTION_ID"}
    assert (AWS_ENV_NAMES | UV_ENV_NAMES).isdisjoint(server["env"])
