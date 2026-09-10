import ast
import re
import tomllib
from pathlib import Path

import pytest

from headless_codex.adapters.secondary.codex.codex_harness import EXECUTION_PROFILE, prepare_codex_home
from headless_codex.ports.interfaces.execution_store import ExecutionTarget
from headless_codex.services.execution_capabilities import render_observation_wait_guidance
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
        "wait_for_post_action_metrics",
        "record_step_outcome",
        "record_resolution",
    ]
    assert EXECUTION_OPERATOR_CONFIG["sandbox_mode"] == "read-only"


@pytest.mark.asyncio
async def test_required_rendered_execution_catalog_exposes_the_actual_fixed_wait_tool(tmp_path):
    """Read the real MCP catalog without running a command or connecting to AWS/model servers."""
    from headless_codex.execution_mcp_server import mcp

    config = tomllib.loads(prepare_codex_home(tmp_path, EXECUTION_PROFILE).read_text())
    server = config["mcp_servers"]["playbook-execution"]
    catalog = {tool.name: tool for tool in await mcp.list_tools()}
    assert server["required"] is True
    assert server["tool_timeout_sec"] == 360
    assert (
        set(server["enabled_tools"])
        == set(catalog)
        == {
            "run_playbook_command",
            "wait_for_post_action_metrics",
            "record_step_outcome",
            "record_resolution",
        }
    )
    schema = catalog["wait_for_post_action_metrics"].parameters
    required = {
        "step_id",
        "action_step_id",
        "metrics",
        "failure_alarm_name",
        "region",
    }
    assert set(schema["required"]) == required
    assert set(schema["properties"]) == required | {
        "max_wait_seconds",
        "latency_alarm_name",
        "completed_work_evidence",
    }
    assert schema["properties"]["metrics"]["type"] == "object"
    assert schema["properties"]["max_wait_seconds"]["type"] == "integer"
    assert schema["properties"]["max_wait_seconds"]["default"] == 300
    assert schema["properties"]["latency_alarm_name"]["default"] == ""
    assert schema["properties"]["completed_work_evidence"]["default"] is None
    for name in required - {"metrics"}:
        assert schema["properties"][name]["type"] == "string"


@pytest.mark.asyncio
async def test_documented_wait_calls_match_actual_catalog_arguments_and_defaults():
    """Validate callable examples against MCP metadata, not copied prose expectations."""
    from headless_codex.execution_mcp_server import mcp

    catalog = {tool.name: tool for tool in await mcp.list_tools()}
    schema = catalog["wait_for_post_action_metrics"].parameters
    for guidance in (
        render_observation_wait_guidance(),
        EXECUTION_GUIDANCE,
        EXECUTION_OPERATOR_GUIDANCE,
    ):
        documented = re.search(r"wait_for_post_action_metrics\([^)]*\)", guidance)
        assert documented is not None
        call = ast.parse(" ".join(documented.group().split()), mode="eval").body
        assert isinstance(call, ast.Call)
        assert [arg.id for arg in call.args] == schema["required"]
        assert {kw.arg for kw in call.keywords} == set(schema["properties"]) - set(schema["required"])
        for keyword in call.keywords:
            assert ast.literal_eval(keyword.value) == schema["properties"][keyword.arg]["default"]


def test_packaged_operator_allowlist_matches_required_execution_profile():
    operator = tomllib.loads((EXECUTION_DIR / "agents" / "execution-operator.toml").read_text())
    assert (
        operator["mcp_servers"]["playbook-execution"]["enabled_tools"]
        == (EXECUTION_CONFIG["mcp_servers"]["playbook-execution"]["enabled_tools"])
    )
    assert operator["mcp_servers"]["playbook-execution"]["tool_timeout_sec"] == 360


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

    assert render_observation_wait_guidance() in prompt
    assert "현재 검증 step_id에서 list-metrics와" in prompt
    assert "describe-alarms를 먼저 기록하고 `wait_for_post_action_metrics`를 호출" in prompt


@pytest.mark.parametrize("guidance", [EXECUTION_GUIDANCE, EXECUTION_OPERATOR_GUIDANCE], ids=["root", "operator"])
def test_packaged_execution_guidance_preserves_fixed_wait_contract(guidance):
    """Both operator entry points must avoid rolling windows and read-as-write false positives."""
    for contract in (
        "wait_for_post_action_metrics(step_id, action_step_id, metrics, failure_alarm_name,",
        "region, max_wait_seconds=300, latency_alarm_name='', completed_work_evidence=None)",
        "승인된 현재 검증 step_id",
        "`run_playbook_command`",
        "`aws cloudwatch list-metrics`",
        "`aws cloudwatch describe-alarms`",
        "namespace, metric_name, dimensions",
        "같은 namespace·dimensions·region",
        "threshold·statistic·unit",
        "CLI gate",
        "첫 성공 실제 ECS StopTask",
        "ended_at",
        "floor(epoch/60)*60+60",
        "정확한 분 경계에 끝나도 반드시 다음 분",
        "조회 전에 고정",
        "동일 요청 재호출",
        "다른 인자·앵커는 거부",
        "attempts Sum>0",
        "failures Sum==0",
        "실제 쿼리 SampleCount>0",
        "attempts-failures",
        "successful_writes",
        "실제 명령 증거나 승인 문맥",
        "latency를 생략하고 latency_alarm_name=''",
        "실제 쓰기 작업의 성공을 별도로",
        "읽기 성공은 쓰기 성공이 아니다",
        "누락은 0이 아니며",
        "UTC 시각과 출력",
        "observedAt은 각 명령 결과가 돌아온 뒤",
        "다른 지표가 누락되어도",
        "nonfinite·중복",
        "최종 실패",
        "최대 300초",
        "남은 기존 실행 예산",
        "MCP timeout 360초",
        "취소·claim·명령 timeout",
        "busy-poll",
        "같은 소유자의 해제·롤백",
        "criteria_met=false, resolved=false",
    ):
        assert contract in " ".join(guidance.split())
    assert "임계치를 넘고 있다면 관측 구간을" not in guidance


def test_execution_prompt_preserves_approved_write_only_criterion_without_latency_metadata():
    """Optional latency does not require extra approved fields or mutate the approved plan."""
    criterion = "Completed writes > 0; failures = 0; observed failure alarm OK"
    step = {
        "step_id": "step4",
        "intent": "verify",
        "action": "Observe the approved write operation after release",
        "success_criteria": criterion,
    }
    target = ExecutionTarget(
        rca_id="rca-1",
        engine="headless-codex",
        alarm_name="observed-failure-alarm",
        playbook={"execution_steps": [step.copy()]},
    )
    prompt = build_execution_prompt(target, execution_id="exec-1")
    assert f"- 성공 판정 기준: {criterion}" in prompt
    assert target.playbook["execution_steps"] == [step]
    assert render_observation_wait_guidance() in prompt
    assert "latency는 승인 기준에 있을 때만" in prompt


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
