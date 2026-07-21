import json
import re
from pathlib import Path

import pytest

from cc_headless.ports.dto.models import AlarmContext
from cc_headless.services.prompt_builder import build_prompt

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
SKILLS_DIR = PACKAGE_ROOT / ".claude" / "skills"
MCP_CONFIG = json.loads((PACKAGE_ROOT / "mcp-config.json").read_text())

EXPECTED_SKILLS = {
    "evidence-patterns",
    "hypothesis-generation",
    "hypothesis-tree",
    "hypothesis-validation",
    "progress-reporting",
    "remediation",
}
EXPECTED_SERVERS = {"aws-knowledge", "cloudwatch", "cloudtrail", "github", "rca-progress"}
EXPECTED_CATEGORIES = {"DEPLOYMENT", "INFRASTRUCTURE", "TRAFFIC", "DEPENDENCY", "CONFIGURATION"}
CANONICAL_ARTIFACTS = {
    "scoping.json",
    "hypotheses.json",
    "validation-{N}.json",
    "playbook.json",
    "report.md",
}


def _skill_name(path: Path) -> str:
    match = re.search(r"^name:\s*(\S+)\s*$", path.read_text(), re.MULTILINE)
    assert match, f"missing skill name in {path}"
    return match.group(1)


def _all_guidance() -> str:
    paths = [PACKAGE_ROOT / "CLAUDE.md", *sorted(PROMPTS_DIR.rglob("*.md")), *sorted(SKILLS_DIR.rglob("SKILL.md"))]
    return "\n".join(path.read_text() for path in paths)


def test_skill_directories_have_unique_matching_frontmatter_names():
    paths = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    names = [_skill_name(path) for path in paths]

    assert set(names) == EXPECTED_SKILLS
    assert len(names) == len(set(names))
    assert all(path.parent.name == _skill_name(path) for path in paths)


def test_mcp_server_set_is_explicit_and_stable():
    assert set(MCP_CONFIG["mcpServers"]) == EXPECTED_SERVERS


def test_rca_progress_mcp_points_to_packaged_server():
    config = MCP_CONFIG["mcpServers"]["rca-progress"]

    assert config["command"] == "python"
    assert config["args"][:3] == ["-m", "fastmcp", "run"]
    assert config["args"][3].endswith("/src/cc_headless/mcp_server.py:mcp")


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
        "prompts/sections/stages/2-hypothesis-generation.md",
        ".claude/skills/hypothesis-generation/SKILL.md",
        ".claude/skills/hypothesis-tree/SKILL.md",
    ],
)
def test_hypothesis_guidance_uses_shared_category_vocabulary(relative_path):
    text = (PACKAGE_ROOT / relative_path).read_text()

    assert "APPLICATION" not in text
    assert EXPECTED_CATEGORIES.issubset(set(re.findall(r"\b[A-Z][A-Z_]+\b", text)))


def test_rca_progress_skill_only_documents_implemented_tool_names():
    text = (SKILLS_DIR / "progress-reporting" / "SKILL.md").read_text()
    documented = set(re.findall(r"^### `([a-z_]+)\(", text, re.MULTILINE))

    assert documented == {"save_artifact"}


def test_prompt_forbids_shell_and_unmanaged_file_writes():
    workspace_guidance = (PACKAGE_ROOT / "CLAUDE.md").read_text()

    assert "셸 명령 금지" in workspace_guidance
    assert "그 외 파일 생성·수정·삭제 불가" in workspace_guidance


def test_guidance_requires_fresh_execution_artifacts_and_forbids_prior_run_reuse():
    guidance = _all_guidance()

    assert "각 호출은 빈 실행별 산출물 디렉터리에서 시작하는 독립 RCA이다" in guidance
    assert "이전 호출의 산출물을 재사용하지 않는다" in guidance
    assert "기존 산출물이 있는지 확인" not in guidance
    assert "이전 단계의 산출물이 있으면 해당 내용을 기반으로 이어서 작업" not in guidance
    assert "/tmp/rca-{RCA_ID}" not in guidance


def test_remediation_has_no_registered_write_capability_and_does_not_require_execution(monkeypatch):
    from cc_headless.adapters.secondary.cc.cc_subprocess_runner import _ALLOWED_TOOLS

    registered = set(MCP_CONFIG["mcpServers"])
    write_servers = {"remediation", "healthcare-remediation", "ecs", "http"}
    write_tools = {"update_service", "force_new_deployment", "post", "remediate"}
    assert registered.isdisjoint(write_servers)
    assert not any(any(marker in tool.lower() for marker in write_tools) for tool in _ALLOWED_TOOLS)

    monkeypatch.setattr("cc_headless.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
    prompt = build_prompt(AlarmContext(alarm_name="ReadOnlyRemediationContract"))
    forbidden_directives = {
        "자동 복구 (직접 수행)",
        "복구 검증 (직접 수행)",
        "자동 복구를 시도",
        "엔드포인트를 호출한다",
        "ECS 강제 새 배포를 시도",
        "복구 후 30초 대기",
        "실제 실행한 즉각 조치",
    }

    assert all(directive not in prompt for directive in forbidden_directives)
    assert "서비스·인프라 변경, 대기, 사후 검증을 실행하지 않는다" in prompt
    assert "실행 상태: CC Headless 미실행" in prompt


@pytest.mark.parametrize(
    "relative_path",
    [
        "CLAUDE.md",
        ".claude/skills/remediation/SKILL.md",
        "prompts/sections/stages/8-report.md",
        "prompts/sections/stages/9-playbook.md",
        "prompts/sections/stages/10-remediation.md",
        "prompts/sections/artifacts/playbook.md",
    ],
)
def test_remediation_guidance_records_required_recommendation_controls(relative_path):
    text = (PACKAGE_ROOT / relative_path).read_text()

    for required in ("제안할 액션", "사전조건", "승인 필요", "롤백 조건"):
        assert required in text


@pytest.mark.parametrize(
    "relative_path",
    [
        ".claude/skills/remediation/SKILL.md",
        "prompts/sections/stages/8-report.md",
        "prompts/sections/stages/9-playbook.md",
        "prompts/sections/stages/11-verification.md",
        "prompts/sections/artifacts/playbook.md",
    ],
)
def test_verification_guidance_records_metrics_and_quantitative_decision_criteria(relative_path):
    text = (PACKAGE_ROOT / relative_path).read_text()

    assert "검증 메트릭" in text
    assert "Pass" in text
    assert "Fail" in text


def test_verification_plan_uses_registered_observability_capability_without_running_remediation():
    verification = (PROMPTS_DIR / "sections" / "stages" / "11-verification.md").read_text()

    assert "cloudwatch" in MCP_CONFIG["mcpServers"]
    assert "메트릭" in verification
    assert "사후 검증을 직접 수행하지 않는다" in verification


def test_required_artifact_contract_matches_watcher():
    from cc_headless.services.artifact_watcher import ARTIFACT_SPAN_MAP

    assert set(ARTIFACT_SPAN_MAP) == {"scoping.json", "hypotheses.json", "playbook.json", "report.md"}
    assert "validation-{N}.json" in _all_guidance()
