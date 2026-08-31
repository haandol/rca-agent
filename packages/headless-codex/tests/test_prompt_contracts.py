import re
import tomllib
from pathlib import Path

import pytest

from headless_codex.ports.dto.models import AlarmContext
from headless_codex.services.prompt_builder import build_prompt

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
SKILLS_DIR = PACKAGE_ROOT / "harness" / "skills"
ANALYSIS_DIR = PACKAGE_ROOT / "harness" / "analysis"
AGENTS_DIR = ANALYSIS_DIR / "agents"
ANALYSIS_CONFIG = tomllib.loads((ANALYSIS_DIR / "config.toml").read_text())
RCA_AGENT_CONFIG = tomllib.loads((AGENTS_DIR / "rca-specialist.toml").read_text())
REPORT_AGENT_CONFIG = tomllib.loads((AGENTS_DIR / "report-specialist.toml").read_text())

EXPECTED_SKILLS = {
    "evidence-patterns",
    "hypothesis-generation",
    "hypothesis-tree",
    "hypothesis-validation",
    "progress-reporting",
    "reporting",
}
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
        ANALYSIS_DIR / "AGENTS.md",
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


def test_role_agents_enforce_distinct_tool_boundaries():
    rca_servers = RCA_AGENT_CONFIG["mcp_servers"]
    report_servers = REPORT_AGENT_CONFIG["mcp_servers"]

    assert set(rca_servers) == {"aws-knowledge", "cloudwatch", "cloudtrail", "github", "rca-progress"}
    assert set(report_servers) == {"rca-progress"}
    assert rca_servers["rca-progress"]["enabled_tools"] == ["save_analysis_artifact"]
    assert report_servers["rca-progress"]["enabled_tools"] == ["save_report_artifact"]


def test_no_role_agent_can_change_a_service():
    for config in (RCA_AGENT_CONFIG, REPORT_AGENT_CONFIG):
        assert config["sandbox_mode"] == "read-only"
        assert not any("playbook-execution" in name for name in config["mcp_servers"])


def test_rca_progress_mcp_points_to_packaged_server():
    config = RCA_AGENT_CONFIG["mcp_servers"]["rca-progress"]

    assert config["command"] == "fastmcp"
    assert config["args"][0] == "run"
    assert config["args"][1] == "{{PACKAGE_ROOT}}/src/headless_codex/mcp_server.py:mcp"


def test_agent_configs_have_no_environment_specific_absolute_paths():
    """The same harness must start from a local checkout and from the image."""
    for config in (RCA_AGENT_CONFIG, REPORT_AGENT_CONFIG):
        for name, server in config["mcp_servers"].items():
            for arg in server.get("args", []):
                assert not arg.startswith("/"), f"{name} pins a deployment-specific path: {arg}"
            for key, value in server.get("env", {}).items():
                assert not value.startswith("/"), f"{name} env {key} pins a deployment-specific path: {value}"


def test_mcp_servers_do_not_require_offline_uv_dependency_resolution():
    knowledge = RCA_AGENT_CONFIG["mcp_servers"]["aws-knowledge"]
    progress = RCA_AGENT_CONFIG["mcp_servers"]["rca-progress"]

    assert knowledge["command"] == "fastmcp"
    assert progress["command"] == "fastmcp"
    assert "uvx" not in {knowledge["command"], progress["command"]}
    assert "-m" not in progress["args"]


def test_github_mcp_is_limited_to_read_only_toolsets():
    env = RCA_AGENT_CONFIG["mcp_servers"]["github"]["env"]
    toolsets = set(env["GITHUB_TOOLSETS"].split(","))

    assert toolsets == {"repos", "pull_requests"}
    assert toolsets.isdisjoint({"issues", "actions", "projects"})
    assert env["GITHUB_READ_ONLY"] == "1"


def test_prompt_resolves_every_include(monkeypatch):
    monkeypatch.setattr("headless_codex.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)

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
        "harness/skills/hypothesis-generation/SKILL.md",
        "harness/skills/hypothesis-tree/SKILL.md",
    ],
)
def test_hypothesis_guidance_uses_shared_category_vocabulary(relative_path):
    text = (PACKAGE_ROOT / relative_path).read_text()

    assert "APPLICATION" not in text
    assert EXPECTED_CATEGORIES.issubset(set(re.findall(r"\b[A-Z][A-Z_]+\b", text)))


def test_rca_progress_skill_only_documents_implemented_storage_tools():
    text = (SKILLS_DIR / "progress-reporting" / "SKILL.md").read_text()
    documented = set(re.findall(r"^### `([a-z_]+)\(", text, re.MULTILINE))

    assert documented == {"save_analysis_artifact", "save_report_artifact"}


def test_prompt_forbids_shell_arbitrary_http_ecs_and_unmanaged_file_writes():
    workspace_guidance = (ANALYSIS_DIR / "AGENTS.md").read_text()

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
    monkeypatch.setattr("headless_codex.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
    prompt = build_prompt(AlarmContext(alarm_name="OrchestrationContract"))

    assert prompt.index("1단계: RCA 전문 에이전트") < prompt.index("2단계: Report 전문 에이전트")
    assert "RCA가 미확정이어도 호출한다" in prompt
    assert "2단계: 조건부 Remediation" not in prompt


def test_compiled_prompt_and_workspace_wait_for_terminal_specialist_failure(monkeypatch):
    monkeypatch.setattr("headless_codex.services.prompt_builder._PROMPTS_DIR", PROMPTS_DIR)
    prompt = build_prompt(AlarmContext(alarm_name="NonInteractiveContract"))
    workspace = (ANALYSIS_DIR / "AGENTS.md").read_text()

    for guidance in (prompt, workspace):
        normalized = " ".join(guidance.split())
        assert "이 워커는 비대화형이다" in normalized
        assert "사용자 입력이나 진행 여부를 요청하지 않고" in normalized
        assert "진행 중이거나 background에서 실행 중인 전문 에이전트 호출은 실패가 아니다" in normalized
        assert "산출물 누락이나 경과 시간만으로 실패를 추론하지 않는다" in normalized
        assert "기존 task가 실행 중이면 같은 전문 에이전트를 다시 호출하지 않고" in normalized
        assert "`wait`로 완료를 기다린다" in normalized
        assert "terminal interruption 또는 provider/tool failure를 명시적으로 보고한 뒤에만" in normalized
        assert "동일한 전문 에이전트를 한 번 재호출한다" in normalized
        assert "재시도도 실패하면 실행을 명시적으로 실패시키고 종료한다" in normalized
        assert "누락 산출물을 직접 작성·보완하거나 다른 역할에 대신 작성시키지 않는다" in normalized


def test_orchestrator_waits_for_terminal_notification_before_retry():
    orchestrator = (ANALYSIS_DIR / "AGENTS.md").read_text()
    normalized = " ".join(orchestrator.split())

    assert "사용자 입력이나 진행 여부를 요청하지 않고" in normalized
    assert "background에서 실행 중인 전문 에이전트 호출은 실패가 아니다" in normalized
    assert "산출물 누락이나 경과 시간만으로 실패를 추론하지 않는다" in normalized
    assert "`wait`로 완료를 기다린다" in normalized
    assert "동일한 전문 에이전트를 한 번 재호출한다" in normalized
    assert "재시도도 실패하면 실행을 명시적으로 실패시키고 종료한다" in normalized


def test_the_analysis_harness_exposes_no_write_capability():
    assert "mcp_servers" not in ANALYSIS_CONFIG
    assert RCA_AGENT_CONFIG["mcp_servers"]["rca-progress"]["enabled_tools"] == ["save_analysis_artifact"]
    assert REPORT_AGENT_CONFIG["mcp_servers"]["rca-progress"]["enabled_tools"] == ["save_report_artifact"]


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
        PACKAGE_ROOT / "src" / "headless_codex" / "adapters" / "secondary" / "report" / "s3_report_store.py"
    ).read_text()

    assert '"StringValue": "headless_codex_complete"' in report_store
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
    from headless_codex.services.artifact_watcher import ARTIFACT_SPAN_MAP

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
    from headless_codex.services.artifact_validation import _EXECUTION_STEP_FIELDS

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
    from headless_codex.services.artifact_validation import _ISO_TIMESTAMP

    guidance = (SKILLS_DIR / "reporting" / "SKILL.md").read_text().lower()

    for label in ("current alarm window", "historical comparison window"):
        matching = [line for line in guidance.splitlines() if label in line]
        assert any(len(_ISO_TIMESTAMP.findall(line)) >= 2 for line in matching), (
            f"guidance example fails the gate for {label}"
        )
