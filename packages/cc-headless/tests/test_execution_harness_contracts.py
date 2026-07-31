import json
import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_AGENTS_DIR = PACKAGE_ROOT / ".claude-execution" / "agents"
EXECUTION_MCP_CONFIG = json.loads((PACKAGE_ROOT / "execution-mcp-config.json").read_text())
ANALYSIS_MCP_CONFIG = json.loads((PACKAGE_ROOT / "mcp-config.json").read_text())
EXECUTION_GUIDANCE = (PACKAGE_ROOT / "EXECUTION.md").read_text()

EXPECTED_EXECUTION_AGENTS = {
    "execution-orchestrator",
    "execution-operator",
    "retrospective-orchestrator",
    "retrospective-analyst",
}
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

    # 러너의 허용 목록은 루트가 아니라 실제로 도구를 쓰는 하위 에이전트와 맞아야 한다.
    # 루트는 위임만 하므로 Agent 와 Skill 만 더 갖는다.
    assert set(_EXECUTION_TOOLS) == _tools("execution-operator") | {"Agent"}
    assert set(_RETROSPECTIVE_TOOLS) == _tools("retrospective-analyst") | {"Agent"}


def test_the_root_agents_delegate_instead_of_executing():
    """MCP 도구는 위임된 하위 에이전트에서만 해석된다.

    루트에 실행 도구를 적으면 노출되지 않아 아무 절차도 수행되지 않는다. 라이브
    실측에서 확인한 동작이므로 위임 구조를 계약으로 고정한다.
    """
    for root, worker in (
        ("execution-orchestrator", "execution-operator"),
        ("retrospective-orchestrator", "retrospective-analyst"),
    ):
        tools = _tools(root)

        assert tools == {f"Agent({worker})", "Skill"}, root
        assert not [tool for tool in tools if tool.startswith("mcp__")], (
            f"{root} must not hold MCP tools — they would not resolve on a root agent"
        )


@pytest.mark.parametrize("root", ["execution-orchestrator", "retrospective-orchestrator"])
def test_the_root_agents_must_wait_for_the_delegation_to_return(root):
    """루트가 위임을 배경으로 띄우고 먼저 끝내면 하위의 마지막 기록이 유실된다.

    라이브 실측에서 실제로 일어났다. 하위 에이전트가 4절차를 모두 수행하고 관측까지
    기록했는데, 루트가 결과를 기다리지 않고 응답해 프로세스가 종료되면서 해소 여부
    기록만 유실됐다. 판정은 미해결로 정확했지만 실행이 무의미해진다.
    """
    guidance = (EXECUTION_AGENTS_DIR / f"{root}.md").read_text()

    assert "배경 작업으로 띄우지 않는다" in guidance, root
    assert "반환할" in guidance and "기다린" in guidance, root


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


def test_the_image_installs_the_executable_the_gate_allows():
    """게이트가 허용하는 실행 파일은 이미지가 제공해야 한다.

    실행 도구는 argv 를 셸 없이 직접 spawn 하므로 이 바이너리가 없으면 모든 절차가
    spawn 실패로 끝난다. 라이브 실측에서 4절차 전부가 이렇게 실패했고, 단위 테스트는
    러너를 대역으로 바꾸므로 이미지 구성을 검증하지 못했다.
    """
    from cc_headless.services.command_gate import _ALLOWED_EXECUTABLE

    dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text()

    assert f"{_ALLOWED_EXECUTABLE} --version" in dockerfile, (
        f"the image must install and verify {_ALLOWED_EXECUTABLE!r}, otherwise every playbook command fails to spawn"
    )
