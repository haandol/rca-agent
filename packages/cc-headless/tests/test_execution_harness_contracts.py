import json
import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_AGENTS_DIR = PACKAGE_ROOT / ".claude-execution" / "agents"
EXECUTION_MCP_CONFIG = json.loads((PACKAGE_ROOT / "execution-mcp-config.json").read_text())
ANALYSIS_MCP_CONFIG = json.loads((PACKAGE_ROOT / "mcp-config.json").read_text())
EXECUTION_GUIDANCE = (PACKAGE_ROOT / "EXECUTION.md").read_text()

EXPECTED_EXECUTION_AGENTS = {"execution-operator", "retrospective-analyst"}
EXPECTED_EXECUTION_SERVERS = {"cloudwatch", "playbook-execution", "playbook-retrospective"}


def _frontmatter_value(path: Path, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", path.read_text(), re.MULTILINE)
    assert match, f"missing {key} in {path}"
    return match.group(1)


def _tools(name: str) -> set[str]:
    return set(_frontmatter_value(EXECUTION_AGENTS_DIR / f"{name}.md", "tools").split(", "))


def test_execution_agents_have_unique_matching_frontmatter_names():
    paths = sorted(EXECUTION_AGENTS_DIR.glob("*.md"))
    names = [_frontmatter_value(path, "name") for path in paths]

    assert set(names) == EXPECTED_EXECUTION_AGENTS
    assert all(path.stem == _frontmatter_value(path, "name") for path in paths)


def test_the_execution_mcp_server_set_is_explicit_and_stable():
    assert set(EXECUTION_MCP_CONFIG["mcpServers"]) == EXPECTED_EXECUTION_SERVERS


def test_the_analysis_harness_has_no_execution_server():
    """분석은 읽기 전용이다. 실행 도구가 분석 하네스에 들어가면 승인 게이트가 무의미해진다."""
    assert set(ANALYSIS_MCP_CONFIG["mcpServers"]).isdisjoint({"playbook-execution", "playbook-retrospective"})


def test_the_execution_harness_has_no_analysis_artifact_server():
    """실행은 분석 산출물을 쓰지 않는다. 실행이 리포트를 변경하면 안 된다."""
    assert "rca-progress" not in EXECUTION_MCP_CONFIG["mcpServers"]


def test_execution_mcp_config_has_no_environment_specific_absolute_paths():
    for name, server in EXECUTION_MCP_CONFIG["mcpServers"].items():
        for arg in server.get("args", []):
            assert not arg.startswith("/"), f"{name} pins a deployment-specific path: {arg}"
        for key, value in server.get("env", {}).items():
            assert not value.startswith("/"), f"{name} env {key} pins a deployment-specific path: {value}"


@pytest.mark.parametrize("server", ["playbook-execution", "playbook-retrospective"])
def test_the_packaged_execution_servers_are_referenced_through_the_package_root(server):
    config = EXECUTION_MCP_CONFIG["mcpServers"][server]

    assert config["command"] == "fastmcp"
    assert config["args"][0] == "run"
    assert config["args"][1].startswith("{{PACKAGE_ROOT}}/src/cc_headless/")


def test_no_execution_agent_gets_a_shell():
    """명령은 서버가 파싱해 파괴성을 판정한 뒤에만 실행된다. Bash 는 그 경계를 우회한다."""
    for path in sorted(EXECUTION_AGENTS_DIR.glob("*.md")):
        tools = _frontmatter_value(path, "tools")
        assert "Bash" not in tools, path.name


def test_the_execution_operator_can_run_commands_and_must_record_observations():
    tools = _tools("execution-operator")

    assert tools == {
        "Skill",
        "mcp__cloudwatch__*",
        "mcp__playbook-execution__run_playbook_command",
        "mcp__playbook-execution__record_step_outcome",
        "mcp__playbook-execution__record_resolution",
    }


def test_the_retrospective_analyst_cannot_execute_anything():
    tools = _tools("retrospective-analyst")

    assert tools == {"Skill", "mcp__playbook-retrospective__save_playbook_update"}
    assert not any("run_playbook_command" in tool for tool in tools)


def test_the_runner_allowlists_match_the_agent_frontmatter():
    from cc_headless.adapters.secondary.cc.cc_execution_runner import (
        _EXECUTION_TOOLS,
        _RETROSPECTIVE_TOOLS,
    )

    assert set(_EXECUTION_TOOLS) == _tools("execution-operator")
    assert set(_RETROSPECTIVE_TOOLS) == _tools("retrospective-analyst")


def test_the_analysis_harness_cannot_reach_any_execution_tool():
    """읽기 도구는 공유해도 되지만 쓰기 경로는 실행 하네스만 가진다."""
    from cc_headless.adapters.secondary.cc.cc_subprocess_runner import _ALLOWED_TOOLS

    assert not [tool for tool in _ALLOWED_TOOLS if "playbook-execution" in tool]
    assert not [tool for tool in _ALLOWED_TOOLS if "playbook-retrospective" in tool]


def test_the_execution_harness_cannot_reach_the_analysis_artifact_tool():
    from cc_headless.adapters.secondary.cc.cc_execution_runner import (
        _EXECUTION_TOOLS,
        _RETROSPECTIVE_TOOLS,
    )

    assert not [tool for tool in (*_EXECUTION_TOOLS, *_RETROSPECTIVE_TOOLS) if "rca-progress" in tool]


def test_the_execution_guidance_forbids_working_around_a_refusal():
    assert "거부된 명령을 다른 표현으로 다시 시도하지 않는다" in EXECUTION_GUIDANCE
    assert "우회 경로를 찾지 않는다" in EXECUTION_GUIDANCE
    assert "셸 합성" in EXECUTION_GUIDANCE


def test_the_execution_guidance_forbids_declaring_an_unobserved_resolution():
    assert "관측 없이 해결을 선언하지 않는다" in EXECUTION_GUIDANCE
    assert "unobservable_reason" in EXECUTION_GUIDANCE
    assert "관측하지 않은 결과를 관측했다고 기록 금지" in EXECUTION_GUIDANCE


def test_the_execution_guidance_forbids_changing_the_analysis_report():
    assert "분석 리포트 수정 금지" in EXECUTION_GUIDANCE


def test_the_execution_guidance_documents_the_option_ordering_the_gate_requires():
    # 게이트는 서비스·작업 앞의 전역 옵션을 판정 불가로 거부하므로, 지침이 이 형태를
    # 알려주지 않으면 모든 명령이 거부된다.
    assert "작업 이름 뒤에" in EXECUTION_GUIDANCE
    assert "aws <service> <operation>" in EXECUTION_GUIDANCE


def test_the_retrospective_agent_only_corrects_procedure_defects():
    guidance = (EXECUTION_AGENTS_DIR / "retrospective-analyst.md").read_text()

    assert "재시도로 같은 명령이 성공했다면" in guidance
    for transient in ("TRANSIENT", "THROTTLED", "TIMEOUT", "UNKNOWN"):
        assert transient in guidance


def test_the_retrospective_agent_is_told_deletion_never_happens():
    guidance = (EXECUTION_AGENTS_DIR / "retrospective-analyst.md").read_text()

    assert "삭제는 일어나지 않는다" in guidance
    assert "기존 `step_id`를 재사용" in guidance
