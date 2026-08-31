import tomllib
from pathlib import Path

from headless_codex import bedrock_token
from headless_codex.adapters.secondary.codex.codex_harness import (
    ANALYSIS_PROFILE,
    EXECUTION_PROFILE,
    MODEL_EVAL_PROFILE,
    RETROSPECTIVE_PROFILE,
    prepare_codex_home,
    prepare_workspace,
    runtime_home_root,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_every_profile_renders_without_placeholders(tmp_path):
    for profile in (ANALYSIS_PROFILE, MODEL_EVAL_PROFILE, EXECUTION_PROFILE, RETROSPECTIVE_PROFILE):
        home = tmp_path / profile
        home.mkdir()
        config_path = prepare_codex_home(home, profile)
        rendered = [config_path, *sorted((home / "agents").glob("*.toml"))]

        for path in rendered:
            text = path.read_text()
            assert "{{" not in text
            with path.open("rb") as handle:
                tomllib.load(handle)


def test_root_profiles_have_no_mcp_servers(tmp_path):
    for profile in (ANALYSIS_PROFILE, MODEL_EVAL_PROFILE):
        home = tmp_path / profile
        home.mkdir()
        config = tomllib.loads(prepare_codex_home(home, profile).read_text())
        assert "mcp_servers" not in config
        assert config["agents"]["max_threads"] == 2


def test_registered_agents_are_spawned_without_full_history_forks():
    paths = [
        PACKAGE_ROOT / "harness" / "analysis" / "AGENTS.md",
        PACKAGE_ROOT / "prompts" / "sections" / "stages" / "1-rca-agent.md",
        PACKAGE_ROOT / "prompts" / "sections" / "stages" / "2-report-agent.md",
    ]
    for path in paths:
        text = path.read_text()
        assert "fork_context=false" in text
        assert "agent_type=" in text


def test_workspace_guidance_is_profile_specific(tmp_path):
    expected = {
        ANALYSIS_PROFILE: "RCA Orchestrator",
        MODEL_EVAL_PROFILE: "RCA Orchestrator",
        EXECUTION_PROFILE: "플레이북 실행 하네스",
        RETROSPECTIVE_PROFILE: "Retrospective Analyst",
    }
    for profile, title in expected.items():
        workspace = tmp_path / profile
        workspace.mkdir()
        prepare_workspace(workspace, profile)
        assert title in (workspace / "AGENTS.md").read_text()


def test_runtime_codex_home_uses_a_non_temporary_parent(monkeypatch, tmp_path):
    configured = tmp_path / "persistent-codex-home"
    monkeypatch.setenv("CODEX_RUNTIME_HOME_ROOT", str(configured))

    assert runtime_home_root() == configured
    assert configured.is_dir()


def test_image_contains_codex_cli_and_the_single_harness():
    dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text()

    assert "@openai/codex@0.151.0" in dockerfile
    assert "@anthropic-ai/claude-code" not in dockerfile
    assert "COPY harness/ ./harness/" in dockerfile
    assert "COPY .claude/" not in dockerfile
    assert 'CODEX_RUNTIME_HOME_ROOT="/home/codexuser/.codex-runs"' in dockerfile


def test_bedrock_auth_command_prints_only_the_short_lived_token(monkeypatch, capsys):
    monkeypatch.setattr(bedrock_token, "provide_token", lambda: "test-token")

    bedrock_token.main()

    assert capsys.readouterr().out == "test-token\n"
