"""Public role tools enforce ownership, immutability and standalone evaluation isolation."""

import asyncio
import json
import time

import pytest
from fastmcp import Client

from headless_codex import analysis_parts_mcp_server as server
from headless_codex.services import execution_context
from headless_codex.services.analysis_part_workspace import CONTEXT_NAME, activate_role, read_object, write_once


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path / "artifacts")
    work = execution_context.ExecutionContext.create("rca")
    work.prepare()
    monkeypatch.setenv("RCA_EXECUTION_TOKEN", work.token)
    monkeypatch.setattr(server, "_check_owner", lambda: None)
    write_once(
        work.token,
        CONTEXT_NAME,
        {"model_eval": True, "incident": {"alarm": {}, "observations": {}, "source_artifacts": []}},
    )
    return work


def test_public_role_tool_sets_are_separate(workspace):
    async def run():
        expected = [
            (server.recovery, {"read_analysis_context", "save_recovery_result"}),
            (server.code_preview, {"read_analysis_context", "save_code_proposal"}),
            (server.operations, {"read_analysis_context", "save_operations_result", "read_ci_configuration"}),
        ]
        for app, names in expected:
            async with Client(app) as client:
                assert {tool.name for tool in await client.list_tools()} == names

    asyncio.run(run())


def test_same_result_is_idempotent_and_another_role_cannot_replace_it(workspace):
    activate_role(workspace.token, "recovery", time.monotonic() + 30)
    value = {
        "title": "제목",
        "summary": "요약",
        "reason": "사유",
        "recommendation": "UNAVAILABLE",
        "evidence_refs": [],
        "limitations": [],
    }
    assert json.loads(server.save_recovery_result(json.dumps(value)))["ok"]
    assert json.loads(server.save_recovery_result(json.dumps(value)))["ok"]
    assert not json.loads(server.save_recovery_result(json.dumps({**value, "summary": "changed"})))["ok"]
    activate_role(workspace.token, "operations", time.monotonic() + 30)
    assert not json.loads(server.save_recovery_result(json.dumps(value)))["ok"]
    assert read_object(workspace.token, "recovery-result.json") == value


def test_model_eval_cannot_call_live_ci_even_by_direct_tool_name(workspace, monkeypatch):
    from headless_codex.services import operations_sources

    monkeypatch.setattr(operations_sources, "read_ci_source", lambda *_a, **_k: pytest.fail("live query"))
    activate_role(workspace.token, "operations", time.monotonic() + 30)
    assert not json.loads(server.read_ci_configuration("o/r", ".github/workflows/ci.yml", "main"))["ok"]


def test_ops_model_claim_without_actual_ci_source_is_unverified(workspace):
    activate_role(workspace.token, "operations", time.monotonic() + 30)
    value = {
        "title": "ops",
        "summary": "검토",
        "findings": [{"statement": "CI control exists", "status": "OBSERVED", "evidence_refs": ["invented"]}],
        "recommendations": [],
        "limitations": [],
    }
    assert json.loads(server.save_operations_result(json.dumps(value)))["ok"]
    stored = read_object(workspace.token, "operations-result.json")
    assert stored["findings"][0]["status"] == "UNVERIFIED"
    assert stored["limitations"]
