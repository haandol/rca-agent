"""Completed RPC errors remain warnings; unknown transport lifetime still fences validation."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent, Tool
from strands.tools.mcp import MCPClient

from rca_agent.adapters.secondary.evidence.readonly_tools import ReadOnlyTool, ReadOnlyTools
from rca_agent.ports.dto.models import Hypothesis, HypothesisCategory, HypothesisStatus, ScopingResult
from rca_agent.prompts.evidence import EVIDENCE_COLLECTION_SYSTEM_PROMPT
from rca_agent.services import evidence
from rca_agent.services.collected_observations import derive_received_facts, received_tool_warnings
from rca_agent.services.pipeline import ValidationLoopState
from rca_agent.services.validation import ValidationOutput, _JudgmentItem, run_validation

from .test_incident_observation import setup_observations as observation_fixture


@pytest.fixture
def setup_observations():
    """Use the established local reader proof; never contact AWS."""
    return observation_fixture.__wrapped__()


def rpc_error(request_id="optional-code"):
    """Use the installed SDK's actual completed-error mapper, not error-text classification."""
    client = MCPClient(lambda: None)
    return client._handle_tool_result(
        request_id,
        CallToolResult(
            isError=True, content=[TextContent(type="text", text="Repository search failed: 422 Validation Failed")]
        ),
    )


def tool(owner, name):
    """Wrap the same invocation-owned stream entry point used in production."""
    original = SimpleNamespace(
        mcp_tool=Tool(name=name, inputSchema={"type": "object"}), mcp_client=owner.client, tool_name=name
    )
    return ReadOnlyTool(original, owner)


def h():
    return Hypothesis(
        hypothesis_id="candidate",
        description="Installed write references a column absent in the observed schema",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.5,
        required_evidence=["actual write/schema observations"],
    )


def judge(scope, collected):
    """Replay the unmodified failure guard against a model judgment with high confidence."""
    state = ValidationLoopState(
        hypotheses=[h()],
        evidence_map=collected.evidence_map,
        collection_states=collected.collection_states,
        fact_map=collected.fact_map,
        source_ref_map=collected.source_ref_map,
        warning_map=collected.warning_map,
    )
    agent = MagicMock()
    agent.return_value.structured_output = ValidationOutput(
        judgment=_JudgmentItem(
            status=HypothesisStatus.CONFIRMED,
            confidence_score=0.93,
            reasoning="actual source observations",
            evidence_summary=["verified incident source"],
            validated_fault_type="UNSUPPORTED",
        )
    )
    output = run_validation(
        [h()],
        {"candidate": state.model_evidence("candidate")},
        agent,
        evidence_failed_ids=collected.failed_ids,
        scoping_result=scope,
    )
    return output.judgments[0], agent.call_args.args[0], state


def test_actual_sdk_completed_error_marker_differs_from_transport_exception():
    """A returned isError result acknowledges the RPC; a synthetic SDK timeout does not."""
    error = rpc_error()
    owner = ReadOnlyTools(MagicMock(), 60)
    owner.record(error, "search_repositories", {"query": "observed repository"}, expected_tool_use_id="optional-code")
    assert not owner.termination_uncertain
    assert owner.receipts[0]["request_terminated"] is True
    assert received_tool_warnings(owner.receipts)[0]["kind"] == "tool_error"
    synthetic = MCPClient(lambda: None)._handle_tool_execution_error("transport", TimeoutError("socket read"))
    assert "isError" not in synthetic
    owner.record(synthetic, "get_log_events", {}, expected_tool_use_id="transport")
    assert owner.termination_uncertain
    assert received_tool_warnings(owner.receipts)[-1]["kind"] == "request_termination_unknown"


@pytest.mark.parametrize("mode", ["scope-facts", "received-facts"])
def test_optional_rpc_error_keeps_valid_evidence_and_warning_outside_summary(setup_observations, mode):
    """One unavailable repository cannot erase pinned/current facts or downgrade 0.93 solely via failed_ids."""
    reader, alarm, _, _, _, logs, _ = setup_observations
    scope = ScopingResult(
        alarm_summary="local", raw_alarm=alarm, incident_observations=reader.observe(alarm, timeout_seconds=90)
    )
    row = deepcopy(logs.filter_log_events.return_value["events"][0])
    if mode == "received-facts":
        scope.incident_observations.critical_facts = []
    success = {
        "status": "success",
        "isError": False,
        "toolUseId": "actual-log",
        "content": [{"text": json.dumps({"events": [row]})}],
    }
    error = rpc_error()

    class Client:
        async def call_tool_async(self, **kwargs):
            return success if kwargs["name"] == "filter_log_events" else error

    owner = ReadOnlyTools(Client(), 60)

    class Agent:
        _rca_read_tools = [owner]
        messages = []

        async def invoke_async(self, *args, **kwargs):
            if mode == "received-facts":
                async for _ in tool(owner, "filter_log_events").stream(
                    {"toolUseId": "actual-log", "input": {"logGroupName": "/ecs/sensor"}}, {}
                ):
                    pass
            async for _ in tool(owner, "search_repositories").stream(
                {"toolUseId": "optional-code", "input": {"query": "observed repository"}}, {}
            ):
                pass
            return SimpleNamespace(
                structured_output=evidence.EvidenceOutput(
                    logs_evidence="Use actual source observations", combined_summary="x" * 500
                )
            )

    s3 = MagicMock()
    with (
        patch.object(evidence, "create_evidence_collection_agent", return_value=Agent()),
        patch.object(evidence, "S3_EVIDENCE_BUCKET", "evidence"),
    ):
        collected = evidence.run_evidence_collection([h()], scope, rca_id="local-review", s3_client=s3)
    assert collected.failed_ids == set()
    assert collected.collection_states["candidate"].status == evidence.CollectionStatus.COMPLETE
    assert collected.collection_states["candidate"].external_request_finished
    assert owner.active == 0 and not owner.termination_uncertain
    verdict, prompt, state = judge(scope, collected)
    assert verdict.status == HypothesisStatus.CONFIRMED and verdict.confidence_score == 0.93
    warning = collected.warning_map["candidate"][0]
    assert warning["tool_name"] == "search_repositories" and warning["request_terminated"] is True
    assert warning["source_ref"].startswith("mcp-tool-result://search_repositories/optional-code#sha256=")
    assert "422 Validation Failed" in prompt and "collection_warnings" in prompt
    assert state.model_evidence("candidate").index("collection_warnings") > 500
    assert len(collected.evidence_map["candidate"]) == 500
    archived = json.loads(s3.put_object.call_args.kwargs["Body"])
    assert archived["tool_results"]["responses"][-1]["result"]["isError"] is True
    assert collected.source_ref_map["candidate"][0] in prompt
    child = h().model_copy(update={"hypothesis_id": "child", "parent_id": "candidate"})
    child_prompt = evidence._build_user_prompt(
        child,
        scope,
        hypotheses_by_id={"candidate": h()},
        evidence_map=collected.evidence_map,
        fact_map=collected.fact_map,
        source_ref_map=collected.source_ref_map,
        warning_map=collected.warning_map,
    )
    assert warning["source_ref"] in child_prompt and "422 Validation Failed" in child_prompt


@pytest.mark.parametrize(
    "model_output",
    [evidence.EvidenceOutput(), evidence.EvidenceOutput(code_change_evidence="422 Repository unavailable")],
)
def test_rpc_only_without_usable_sources_cannot_confirm(model_output):
    """A completed error RPC and positive model prose do not create required incident evidence."""
    owner = ReadOnlyTools(MagicMock(), 60)

    class Agent:
        _rca_read_tools = [owner]
        messages = []

        async def invoke_async(self, *args, **kwargs):
            owner.record(rpc_error(), "search_repositories", {}, expected_tool_use_id="optional-code")
            return SimpleNamespace(structured_output=model_output)

    scope = ScopingResult(alarm_summary="no source observations")
    s3 = MagicMock()
    with (
        patch.object(evidence, "create_evidence_collection_agent", return_value=Agent()),
        patch.object(evidence, "S3_EVIDENCE_BUCKET", "evidence"),
    ):
        collected = evidence.run_evidence_collection([h()], scope, rca_id="local-review", s3_client=s3)
    assert collected.failed_ids == {"candidate"}
    assert collected.collection_states["candidate"].external_request_finished is True
    assert collected.fact_map["candidate"] == []
    assert judge(scope, collected)[0].status == HypothesisStatus.NEEDS_INVESTIGATION
    assert collected.warning_map["candidate"] and s3.put_object.call_count == 1
    assert "diagnostic" in s3.put_object.call_args.kwargs["Key"]


@pytest.mark.parametrize("mutation", ["synthetic-timeout", "cancelled", "mismatch"])
def test_unknown_lifetime_keeps_failed_guard_even_with_pinned_facts(setup_observations, mutation):
    """A genuine transport/cancellation uncertainty is not relaxed by the completed-error repair."""
    reader, alarm, *_ = setup_observations
    scope = ScopingResult(
        alarm_summary="actual", raw_alarm=alarm, incident_observations=reader.observe(alarm, timeout_seconds=90)
    )
    result = rpc_error()
    if mutation == "synthetic-timeout":
        result = MCPClient(lambda: None)._handle_tool_execution_error("optional-code", TimeoutError("idle"))
    elif mutation == "cancelled":
        result["cancelled"] = True
    else:
        result["toolUseId"] = "foreign-result"
    owner = ReadOnlyTools(MagicMock(), 60)

    class Agent:
        _rca_read_tools = [owner]
        messages = []

        async def invoke_async(self, *args, **kwargs):
            owner.record(result, "search_repositories", {}, expected_tool_use_id="optional-code")
            return SimpleNamespace(structured_output=evidence.EvidenceOutput(logs_evidence="actual logs"))

    with patch.object(evidence, "create_evidence_collection_agent", return_value=Agent()):
        collected = evidence.run_evidence_collection([h()], scope)
    assert collected.failed_ids == {"candidate"}
    assert not collected.collection_states["candidate"].external_request_finished
    assert not collected.collection_states["candidate"].eligible()
    assert collected.warning_map["candidate"][0]["kind"] == "request_termination_unknown"
    assert judge(scope, collected)[0].status == HypothesisStatus.NEEDS_INVESTIGATION


def test_rpc_error_payload_never_becomes_positive_log_facts(setup_observations):
    """Even valid-looking driver facts nested inside an RPC error remain error data."""
    reader, alarm, _, _, _, logs, _ = setup_observations
    scope = ScopingResult(
        alarm_summary="actual", raw_alarm=alarm, incident_observations=reader.observe(alarm, timeout_seconds=90)
    )
    result = rpc_error("logs")
    result["structuredContent"] = {"events": deepcopy(logs.filter_log_events.return_value["events"])}
    owner = ReadOnlyTools(MagicMock(), 60)
    owner.record(result, "filter_log_events", {"logGroupName": "/ecs/sensor"}, expected_tool_use_id="logs")
    assert derive_received_facts(owner.receipts, scope) == []
    assert len(received_tool_warnings(owner.receipts)) == 1


def test_evidence_prompt_uses_verified_identities_and_honest_bounded_coverage():
    """Keep general identity and coverage rules instead of scenario IDs or the removed maintenance story."""
    prompt = EVIDENCE_COLLECTION_SYSTEM_PROMPT
    assert "Never guess a GitHub owner" in prompt
    assert "registration order is not service deployment history" in prompt
    assert "pinned verified pre-incident normal baseline" in prompt
    assert "Query only a specific missing fact" in prompt
    assert "representative sample is not complete window coverage" in prompt
    assert "database PID's owner" in prompt
    assert "maintenance source_manifest" not in prompt and "current blocker" not in prompt
    assert "1-hour window" not in prompt and "24 hours prior" not in prompt
    assert not any(value in prompt for value in ["RcaAgentDev", "healthcare-sensor-app", ":79", ":80"])


@pytest.mark.parametrize("exception", [TimeoutError("transport silence"), asyncio.CancelledError()])
def test_direct_transport_failure_keeps_unknown_guard_and_request_metadata(exception):
    """An exception instead of a correlated RPC result is never reclassified as a completed error."""

    class Client:
        async def call_tool_async(self, **kwargs):
            raise exception

    owner = ReadOnlyTools(Client(), 60)

    async def run():
        with pytest.raises(type(exception)):
            async for _ in tool(owner, "get_log_events").stream(
                {"toolUseId": "failed-read", "input": {"logGroupName": "/local"}}, {}
            ):
                pass

    asyncio.run(run())
    assert owner.active == 0 and owner.termination_uncertain
    warning = received_tool_warnings(owner.receipts)[0]
    assert warning["request_terminated"] is False
    assert warning["requested_tool_use_id"] == "failed-read"
    assert warning["response"]["transport_error_type"] == type(exception).__name__


def test_explicit_success_warning_is_preserved_without_becoming_an_error_or_positive_fact():
    """A successful RPC may still report incomplete coverage independently of its lifetime."""
    owner = ReadOnlyTools(MagicMock(), 60)
    owner.record(
        {
            "status": "success",
            "toolUseId": "query",
            "isError": False,
            "metadata": {"warnings": ["representative sample; not full window"]},
            "content": [],
        },
        "get_log_events",
        {},
        expected_tool_use_id="query",
    )
    warning = received_tool_warnings(owner.receipts)[0]
    assert warning["kind"] == "tool_warning" and warning["request_terminated"] is True
    assert not owner.termination_uncertain
    assert "representative sample" in json.dumps(warning)
