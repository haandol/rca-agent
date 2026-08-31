import tomllib
from pathlib import Path

from headless_codex.ports.interfaces.execution_store import ExecutionTarget
from headless_codex.services.execution_prompt import build_execution_prompt

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PACKAGE_ROOT / "harness" / "analysis"
EXECUTION_DIR = PACKAGE_ROOT / "harness" / "execution"
RETROSPECTIVE_DIR = PACKAGE_ROOT / "harness" / "retrospective"

ANALYSIS_CONFIG = tomllib.loads((ANALYSIS_DIR / "config.toml").read_text())
EXECUTION_CONFIG = tomllib.loads((EXECUTION_DIR / "config.toml").read_text())
RETROSPECTIVE_CONFIG = tomllib.loads((RETROSPECTIVE_DIR / "config.toml").read_text())
EXECUTION_OPERATOR_CONFIG = EXECUTION_CONFIG
RETROSPECTIVE_ANALYST_CONFIG = RETROSPECTIVE_CONFIG
EXECUTION_GUIDANCE = (EXECUTION_DIR / "AGENTS.md").read_text()
EXECUTION_OPERATOR_GUIDANCE = (EXECUTION_DIR / "agents" / "execution-operator.md").read_text()


def test_analysis_orchestrator_has_no_tools_and_direct_workers_hold_only_their_tools():
    assert "mcp_servers" not in ANALYSIS_CONFIG
    assert set(EXECUTION_CONFIG["mcp_servers"]) == {"cloudwatch", "playbook-execution"}
    assert set(RETROSPECTIVE_CONFIG["mcp_servers"]) == {"playbook-retrospective"}


def test_analysis_and_execution_capabilities_are_disjoint():
    rca = tomllib.loads((ANALYSIS_DIR / "agents" / "rca-specialist.toml").read_text())
    report = tomllib.loads((ANALYSIS_DIR / "agents" / "report-specialist.toml").read_text())
    analysis_servers = set(rca["mcp_servers"]) | set(report["mcp_servers"])
    execution_servers = set(EXECUTION_OPERATOR_CONFIG["mcp_servers"])
    retrospective_servers = set(RETROSPECTIVE_ANALYST_CONFIG["mcp_servers"])

    assert analysis_servers.isdisjoint({"playbook-execution", "playbook-retrospective"})
    assert execution_servers.isdisjoint({"rca-progress", "playbook-retrospective"})
    assert retrospective_servers == {"playbook-retrospective"}


def test_execution_operator_has_only_server_gated_execution_tools():
    servers = EXECUTION_OPERATOR_CONFIG["mcp_servers"]

    assert set(servers) == {"cloudwatch", "playbook-execution"}
    assert servers["playbook-execution"]["enabled_tools"] == [
        "run_playbook_command",
        "record_step_outcome",
        "record_resolution",
    ]
    assert EXECUTION_OPERATOR_CONFIG["sandbox_mode"] == "read-only"


def test_retrospective_cannot_execute_commands():
    servers = RETROSPECTIVE_ANALYST_CONFIG["mcp_servers"]

    assert set(servers) == {"playbook-retrospective"}
    assert servers["playbook-retrospective"]["enabled_tools"] == ["save_playbook_update"]
    assert RETROSPECTIVE_ANALYST_CONFIG["sandbox_mode"] == "read-only"


def test_packaged_servers_use_the_package_root_placeholder():
    execution = EXECUTION_OPERATOR_CONFIG["mcp_servers"]["playbook-execution"]
    retrospective = RETROSPECTIVE_ANALYST_CONFIG["mcp_servers"]["playbook-retrospective"]

    assert execution["args"][1] == "{{PACKAGE_ROOT}}/src/headless_codex/execution_mcp_server.py:mcp"
    assert retrospective["args"][1] == "{{PACKAGE_ROOT}}/src/headless_codex/retrospective_mcp_server.py:mcp"


def test_direct_worker_profiles_auto_approve_only_allowlisted_mcp_tools():
    assert EXECUTION_CONFIG["mcp_servers"]["playbook-execution"]["default_tools_approval_mode"] == "approve"
    assert RETROSPECTIVE_CONFIG["mcp_servers"]["playbook-retrospective"]["default_tools_approval_mode"] == "approve"


def test_execution_guidance_forbids_working_around_a_refusal():
    assert "거부된 명령을 다른 표현으로 다시 시도하지 않는다" in EXECUTION_GUIDANCE
    assert "우회 경로를 찾지 않는다" in EXECUTION_GUIDANCE
    assert "셸 합성" in EXECUTION_GUIDANCE


def test_execution_guidance_forbids_declaring_an_unobserved_resolution():
    assert "관측 없이 해결을 선언하지 않는다" in EXECUTION_GUIDANCE
    assert "unobservable_reason" in EXECUTION_GUIDANCE
    assert "관측하지 않은 결과를 관측했다고 기록 금지" in EXECUTION_GUIDANCE


def test_execution_guidance_forbids_changing_the_analysis_report():
    assert "분석 리포트 수정 금지" in EXECUTION_GUIDANCE


def test_execution_guidance_documents_the_option_ordering_the_gate_requires():
    assert "작업 이름 뒤에" in EXECUTION_GUIDANCE
    assert "aws <service> <operation>" in EXECUTION_GUIDANCE


def test_execution_prompt_requires_attempt_evidence_for_verification_only_steps():
    target = ExecutionTarget(
        rca_id="rca-1",
        engine="headless-codex",
        alarm_name="VitalIngestFailure",
        playbook={
            "playbook_id": "pb-1",
            "failure_type": "ingest failure",
            "execution_steps": [
                {
                    "step_id": "verify_ingest_recovery",
                    "intent": "verify recovery",
                    "action": "observe the ingest alarm",
                    "success_criteria": "alarm is OK",
                }
            ],
        },
    )

    prompt = build_execution_prompt(target, execution_id="exec-1")

    for guidance in (prompt, EXECUTION_OPERATOR_GUIDANCE):
        assert "verification-only" in guidance
        assert "읽기 전용 AWS CLI" in guidance
        assert "`run_playbook_command`" in guidance
        assert "CloudWatch MCP" in guidance
        assert "attempt 증거가 아니" in guidance
        assert "missing_attempt_step_ids" in guidance
        assert "missing_outcome_step_ids" in guidance
        assert "`record_resolution`" in guidance and "다시 호출" in guidance


def test_retrospective_only_corrects_procedure_defects():
    guidance = (RETROSPECTIVE_DIR / "agents" / "retrospective-analyst.md").read_text()

    assert "재시도로 같은 명령이 성공했다면" in guidance
    for transient in ("TRANSIENT", "THROTTLED", "TIMEOUT", "UNKNOWN"):
        assert transient in guidance


def test_retrospective_preserves_step_identity_and_never_deletes():
    guidance = (RETROSPECTIVE_DIR / "agents" / "retrospective-analyst.md").read_text()

    assert "삭제는 일어나지 않는다" in guidance
    assert "기존 `step_id`를 재사용" in guidance


def test_image_installs_the_executable_the_gate_allows():
    from headless_codex.services.command_gate import _ALLOWED_EXECUTABLE

    dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text()
    assert f"{_ALLOWED_EXECUTABLE} --version" in dockerfile
