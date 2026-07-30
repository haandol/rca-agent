import json
import re
from pathlib import Path

import pytest

from cc_headless.ports.dto.models import AlarmContext
from cc_headless.services.prompt_builder import build_prompt

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
SKILLS_DIR = PACKAGE_ROOT / ".claude" / "skills"
AGENTS_DIR = PACKAGE_ROOT / ".claude" / "agents"
MCP_CONFIG = json.loads((PACKAGE_ROOT / "mcp-config.json").read_text())

EXPECTED_SKILLS = {
    "evidence-patterns",
    "hypothesis-generation",
    "hypothesis-tree",
    "hypothesis-validation",
    "progress-reporting",
    "reporting",
}
EXPECTED_AGENTS = {"orchestrator", "rca-specialist", "report-specialist"}
EXPECTED_SERVERS = {"aws-knowledge", "cloudwatch", "cloudtrail", "github", "rca-progress"}
EXPECTED_CATEGORIES = {"DEPLOYMENT", "INFRASTRUCTURE", "TRAFFIC", "DEPENDENCY", "CONFIGURATION"}
CANONICAL_ARTIFACTS = {
    "scoping.json",
    "hypotheses.json",
    "validation-{N}.json",
    "playbook.json",
    "report.md",
}


def _frontmatter_value(path: Path, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", path.read_text(), re.MULTILINE)
    assert match, f"missing {key} in {path}"
    return match.group(1)


def _all_guidance() -> str:
    paths = [
        PACKAGE_ROOT / "CLAUDE.md",
        *sorted(PROMPTS_DIR.rglob("*.md")),
        *sorted(SKILLS_DIR.rglob("SKILL.md")),
        *sorted(AGENTS_DIR.glob("*.md")),
    ]
    return "\n".join(path.read_text() for path in paths)


_ISO_TIMESTAMP_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"


def test_skill_directories_have_unique_matching_frontmatter_names():
    paths = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    names = [_frontmatter_value(path, "name") for path in paths]

    assert set(names) == EXPECTED_SKILLS
    assert len(names) == len(set(names))
    assert all(path.parent.name == _frontmatter_value(path, "name") for path in paths)


def test_role_agents_have_unique_matching_frontmatter_names():
    paths = sorted(AGENTS_DIR.glob("*.md"))
    names = [_frontmatter_value(path, "name") for path in paths]

    assert set(names) == EXPECTED_AGENTS
    assert len(names) == len(set(names))
    assert all(path.stem == _frontmatter_value(path, "name") for path in paths)


def test_role_agents_enforce_distinct_tool_boundaries():
    orchestrator_tools = _frontmatter_value(AGENTS_DIR / "orchestrator.md", "tools")
    rca_tools = set(_frontmatter_value(AGENTS_DIR / "rca-specialist.md", "tools").split(", "))
    report_tools = set(_frontmatter_value(AGENTS_DIR / "report-specialist.md", "tools").split(", "))

    assert orchestrator_tools == "Agent(rca-specialist, report-specialist), Skill"
    assert rca_tools == {
        "Skill",
        "mcp__aws-knowledge__*",
        "mcp__cloudwatch__*",
        "mcp__cloudtrail__*",
        "mcp__github__*",
        "mcp__rca-progress__save_artifact",
    }
    assert report_tools == {"Skill", "mcp__rca-progress__save_artifact"}


def test_no_role_agent_can_change_a_service():
    # Analysis is read-only. A write tool in any role would put recovery back
    # inside analysis and bypass the user approval that now gates it.
    for path in sorted(AGENTS_DIR.glob("*.md")):
        tools = _frontmatter_value(path, "tools")
        assert "reset" not in tools, path.name
        assert "Bash" not in tools, path.name


def test_mcp_server_set_is_explicit_and_stable():
    assert set(MCP_CONFIG["mcpServers"]) == EXPECTED_SERVERS


def test_rca_progress_mcp_points_to_packaged_server():
    config = MCP_CONFIG["mcpServers"]["rca-progress"]

    assert config["command"] == "fastmcp"
    assert config["args"][0] == "run"
    assert config["args"][1] == "{{PACKAGE_ROOT}}/src/cc_headless/mcp_server.py:mcp"


def test_mcp_config_has_no_environment_specific_absolute_paths():
    """The same harness must start from a local checkout and from the image."""
    for name, server in MCP_CONFIG["mcpServers"].items():
        for arg in server.get("args", []):
            assert not arg.startswith("/"), f"{name} pins a deployment-specific path: {arg}"
        for key, value in server.get("env", {}).items():
            assert not value.startswith("/"), f"{name} env {key} pins a deployment-specific path: {value}"


def test_mcp_servers_do_not_require_offline_uv_dependency_resolution():
    knowledge = MCP_CONFIG["mcpServers"]["aws-knowledge"]
    progress = MCP_CONFIG["mcpServers"]["rca-progress"]

    assert knowledge["command"] == "fastmcp"
    assert progress["command"] == "fastmcp"
    assert "uvx" not in {knowledge["command"], progress["command"]}
    assert "-m" not in progress["args"]


def test_github_mcp_is_limited_to_read_only_toolsets():
    env = MCP_CONFIG["mcpServers"]["github"]["env"]
    toolsets = set(env["GITHUB_TOOLSETS"].split(","))

    assert toolsets == {"repos", "pull_requests"}
    assert toolsets.isdisjoint({"issues", "actions", "projects"})
    assert env["GITHUB_READ_ONLY"] == "1"


def test_prompt_resolves_every_include(monkeypatch):
    monkeypatch.setattr("cc_headless.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)

    prompt = build_prompt(AlarmContext(alarm_name="ContractAlarm"))

    assert re.search(r"\{\{\s*include:\s*[^}\s]+\.md\s*\}\}", prompt) is None


def test_prompt_and_skills_use_canonical_artifact_extensions():
    guidance = _all_guidance()

    assert "validation-{N}.md" not in guidance
    assert "validation-N.md" not in guidance
    assert "validation-1.md" not in guidance
    for artifact in CANONICAL_ARTIFACTS:
        assert artifact in guidance


@pytest.mark.parametrize(
    "relative_path",
    [
        "prompts/sections/artifacts/hypotheses.md",
        "prompts/sections/artifacts/validation.md",
        ".claude/skills/hypothesis-generation/SKILL.md",
        ".claude/skills/hypothesis-tree/SKILL.md",
    ],
)
def test_hypothesis_guidance_uses_shared_category_vocabulary(relative_path):
    text = (PACKAGE_ROOT / relative_path).read_text()

    assert "APPLICATION" not in text
    assert EXPECTED_CATEGORIES.issubset(set(re.findall(r"\b[A-Z][A-Z_]+\b", text)))


def test_rca_progress_skill_only_documents_implemented_storage_tool():
    text = (SKILLS_DIR / "progress-reporting" / "SKILL.md").read_text()
    documented = set(re.findall(r"^### `([a-z_]+)\(", text, re.MULTILINE))

    assert documented == {"save_artifact"}


def test_prompt_forbids_shell_arbitrary_http_ecs_and_unmanaged_file_writes():
    workspace_guidance = (PACKAGE_ROOT / "CLAUDE.md").read_text()

    assert "셸 명령 금지" in workspace_guidance
    assert "임의 HTTP 요청 금지" in workspace_guidance
    assert "ECS `UpdateService`" in workspace_guidance
    assert "임의 파일 생성·수정·삭제" in workspace_guidance


def test_guidance_requires_fresh_execution_artifacts_and_forbids_prior_run_reuse():
    guidance = _all_guidance()

    assert "각 호출은 빈 실행별 산출물 디렉터리에서 시작하는 독립 RCA이다" in guidance
    assert "이전 호출의 산출물을 재사용하지 않는다" in guidance
    assert "기존 산출물이 있는지 확인" not in guidance
    assert "/tmp/rca-{RCA_ID}" not in guidance


def test_main_prompt_orders_rca_before_mandatory_report(monkeypatch):
    monkeypatch.setattr("cc_headless.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
    prompt = build_prompt(AlarmContext(alarm_name="OrchestrationContract"))

    assert prompt.index("1단계: RCA 전문 에이전트") < prompt.index("2단계: Report 전문 에이전트")
    assert "RCA가 미확정이어도 호출한다" in prompt
    assert "2단계: 조건부 Remediation" not in prompt


def test_the_analysis_harness_exposes_no_write_capability():
    from cc_headless.adapters.secondary.cc.cc_subprocess_runner import _ALLOWED_TOOLS

    write_tools = {tool for tool in _ALLOWED_TOOLS if "rca-progress" in tool and "save_artifact" not in tool}

    assert write_tools == set()


def test_guidance_states_that_execution_is_a_separate_approved_step():
    guidance = _all_guidance()

    assert "사용자 승인" in guidance or "사용자가 승인" in guidance
    assert "별도 실행 에이전트" in guidance or "별도 에이전트" in guidance
    assert "복구를 실행하지 않는다" in guidance


def test_hypothesis_and_validation_contract_require_matching_fault_enum():
    hypotheses = (PROMPTS_DIR / "sections" / "artifacts" / "hypotheses.md").read_text()
    validation = (PROMPTS_DIR / "sections" / "artifacts" / "validation.md").read_text()
    rca_agent = (AGENTS_DIR / "rca-specialist.md").read_text()

    for fault_type in ("db-leak", "high-cpu", "high-memory", "slow-query", "unsupported"):
        assert fault_type in hypotheses
        assert fault_type in rca_agent
    assert "참조하는 hypothesis의 구조화 enum과 정확히" in validation


def test_cc_completion_event_cannot_enter_strands_external_remediation_queue():
    report_store = (
        PACKAGE_ROOT / "src" / "cc_headless" / "adapters" / "secondary" / "report" / "s3_report_store.py"
    ).read_text()

    assert '"StringValue": "cc_headless_complete"' in report_store
    assert '"StringValue": "rca_complete"' not in report_store


def test_report_guidance_marks_the_playbook_as_an_unverified_draft():
    guidance = (SKILLS_DIR / "reporting" / "SKILL.md").read_text()
    playbook = (PROMPTS_DIR / "sections" / "artifacts" / "playbook.md").read_text()

    assert "DRAFT" in guidance
    assert "DRAFT" in playbook
    assert "검증 메트릭" in playbook
    assert "Pass" in playbook
    assert "Fail" in playbook
    # Nothing may describe an execution that has not happened.
    assert "복구를 실행하지 않았으므로" in guidance


def test_report_contract_separates_current_and_historical_evidence_windows():
    guidance = (SKILLS_DIR / "reporting" / "SKILL.md").read_text()
    agent = (AGENTS_DIR / "report-specialist.md").read_text()
    principles = (PROMPTS_DIR / "sections" / "core" / "principles.md").read_text()
    rca_agent = (AGENTS_DIR / "rca-specialist.md").read_text()
    evidence = (SKILLS_DIR / "evidence-patterns" / "SKILL.md").read_text()
    combined = "\n".join((guidance, agent, principles, rca_agent, evidence))

    assert "Current alarm window" in combined
    assert "Historical comparison window" in combined
    assert "ISO-8601" in guidance
    assert "수동 테스트 로그" in combined
    assert "현재 장애 증거" in combined
    assert "시각이 없거나 window를" in guidance
    assert "현재 장애 증거에서 제외" in guidance
    assert "current alarm window 이전의 수동 테스트" in rca_agent
    assert "현재 장애의 발생, 원인, 지속 증거로 사용하지 않는다" in evidence


def test_required_artifact_contract_matches_watcher():
    from cc_headless.services.artifact_watcher import ARTIFACT_SPAN_MAP

    assert set(ARTIFACT_SPAN_MAP) == {
        "scoping.json",
        "hypotheses.json",
        "playbook.json",
        "report.md",
    }
    assert "validation-{N}.json" in _all_guidance()


def test_playbook_guidance_defines_the_execution_step_contract():
    # These are what the execution agent runs and what the retrospective corrects,
    # so the prompt has to name every field the gate requires.
    from cc_headless.services.artifact_validation import _EXECUTION_STEP_FIELDS

    playbook = (PROMPTS_DIR / "sections" / "artifacts" / "playbook.md").read_text()

    for field in _EXECUTION_STEP_FIELDS:
        assert field in playbook
    assert "명령 문자열을 박아 넣지 않는다" in playbook
    assert "되돌릴 수 없는 조치를 담지 않는다" in playbook


def test_playbook_guidance_names_every_required_field_as_mandatory():
    playbook = (PROMPTS_DIR / "sections" / "artifacts" / "playbook.md").read_text()

    assert "모든 키는 필수" in playbook
    for field in ("severity_criteria", "escalation_criteria", "symptom_pattern"):
        assert field in playbook


def test_report_guidance_shows_the_exact_evidence_window_line_the_gate_accepts():
    # The completion gate scans for two ISO-8601 timestamps on the same line as
    # each label, so guidance that only says "write ISO-8601" is not enough.
    guidance = (SKILLS_DIR / "reporting" / "SKILL.md").read_text()

    assert "같은 한 줄" in guidance
    for label in ("Current alarm window", "Historical comparison window"):
        example = next(
            (line for line in guidance.splitlines() if label in line and line.count("T") >= 2),
            "",
        )
        assert example, f"guidance must show a one-line example for {label}"
        assert len(re.findall(_ISO_TIMESTAMP_PATTERN, example)) >= 2


def test_report_guidance_example_passes_the_completion_gate_rule():
    from cc_headless.services.artifact_validation import _ISO_TIMESTAMP

    guidance = (SKILLS_DIR / "reporting" / "SKILL.md").read_text().lower()

    for label in ("current alarm window", "historical comparison window"):
        matching = [line for line in guidance.splitlines() if label in line]
        assert any(len(_ISO_TIMESTAMP.findall(line)) >= 2 for line in matching), (
            f"guidance example fails the gate for {label}"
        )
