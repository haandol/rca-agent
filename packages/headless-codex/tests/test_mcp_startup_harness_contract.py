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
    EXECUTION_PROFILE: {"playbook-execution"},
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
CW_TOOLS = [
    "get_active_alarms",
    "get_alarm_history",
    "get_metric_data",
    "describe_log_groups",
    "execute_log_insights_query",
    "get_logs_insight_query_results",
    "execute_cwl_insights_batch",
]


def _render(tmp_path, profile):
    """Read the actual rendered runtime config, not a parallel test-only template."""
    home = tmp_path / profile
    home.mkdir()
    path = prepare_codex_home(home, profile)
    return home, tomllib.loads(path.read_text())


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            ANALYSIS_RCA_PROFILE,
            {
                "cloudwatch": CW_TOOLS,
                "cloudtrail": ["lookup_events"],
                "github": ["get_file_contents", "get_commit", "list_commits", "search_code", "pull_request_read"],
                "aws-knowledge": ["aws___search_documentation", "aws___read_documentation"],
                "rca-progress": ["save_analysis_artifact", "inspect_ecs_task_control"],
            },
        ),
        (
            EXECUTION_PROFILE,
            {
                "playbook-execution": [
                    "run_playbook_command",
                    "wait_for_post_action_metrics",
                    "wait_for_service_deployment",
                    "record_step_outcome",
                    "record_resolution",
                ],
            },
        ),
    ],
)
def test_generated_role_catalogs_expose_only_reviewed_evidence_and_artifact_tools(tmp_path, profile, expected):
    """Preserve metric/log polling, change lookup and source reading without whole-server catalogs."""
    _, config = _render(tmp_path, profile)
    servers = config["mcp_servers"]
    assert set(servers) == set(expected)
    for name, tools in expected.items():
        assert servers[name]["enabled_tools"] == tools
        assert len(tools) == len(set(tools))
        assert servers[name].get("disabled_tools", []) == []
    if profile == ANALYSIS_RCA_PROFILE:
        assert servers["github"]["env"]["GITHUB_READ_ONLY"] == "1"
        assert servers["github"]["env"]["GITHUB_TOOLSETS"] == "repos,pull_requests"


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
        assert server["tool_timeout_sec"] == (1200 if name == "playbook-execution" else 120)
        assert server["default_tools_approval_mode"] == "approve"


@pytest.mark.parametrize(
    ("profile", "server_name"),
    [
        (ANALYSIS_RCA_PROFILE, "cloudwatch"),
        (ANALYSIS_RCA_PROFILE, "cloudtrail"),
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
    """CLI retains the existing AWS credential environment plus its explicit locked ECS model path."""
    _, config = _render(tmp_path, EXECUTION_PROFILE)
    execution = config["mcp_servers"]["playbook-execution"]
    assert "cloudwatch" not in config["mcp_servers"]
    playbook_names = {
        "PLAYBOOK_EXECUTION_TOKEN",
        "PLAYBOOK_EXECUTION_ID",
        "PLAYBOOK_APPROVED_STEP_IDS",
        "PLAYBOOK_APPROVED_SUCCESS_CRITERIA",
    }
    names = execution["env_vars"]
    assert len(names) == len(set(names))
    assert set(names) == playbook_names | AWS_ENV_NAMES | {"AWS_DATA_PATH"}
    assert (AWS_ENV_NAMES | UV_ENV_NAMES).isdisjoint(execution["env"])


def test_retrospective_reader_inherits_aws_role_environment_names_only(tmp_path):
    """Retrospective reads fixed S3 evidence through inherited role names, never credential values."""
    _, config = _render(tmp_path, RETROSPECTIVE_PROFILE)
    server = config["mcp_servers"]["playbook-retrospective"]
    assert (
        set(server["env_vars"])
        == {"PLAYBOOK_EXECUTION_TOKEN", "PLAYBOOK_EXECUTION_ID", "S3_EVIDENCE_BUCKET"} | AWS_ENV_NAMES
    )
    assert (AWS_ENV_NAMES | UV_ENV_NAMES).isdisjoint(server["env"])
