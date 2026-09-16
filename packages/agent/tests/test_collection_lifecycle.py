from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from rca_agent.ports.dto.models import Hypothesis, HypothesisCategory, ScopingResult
from rca_agent.services.evidence import (
    CollectionAttempt,
    CollectionStatus,
    EvidenceCollectionResult,
    EvidenceOutput,
    collect_evidence,
    collection_is_due,
    run_evidence_collection,
)
from rca_agent.utils.agent_invocation import InvocationStoppedError, invocation_scope, invoke_agent


def stop_after(seconds):
    """Use an explicit stop signal, independently of the model's work-start budget."""
    stop_at = time.monotonic() + seconds
    return lambda: time.monotonic() < stop_at


def hypothesis(name="h1"):
    return Hypothesis(
        hypothesis_id=name,
        description=name,
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.5,
    )


def result(name="h1", **kwargs):
    return EvidenceCollectionResult(hypothesis_id=name, summary="observed", full_evidence="observed", **kwargs)


def test_unstarted_is_not_failed_and_can_start_in_later_batch():
    states = {}
    scope = ScopingResult(alarm_summary="incident")
    with patch("rca_agent.services.evidence.collect_evidence", return_value=result()) as collect:
        summary = run_evidence_collection([hypothesis()], scope, timeout_seconds=0, collection_states=states)
        collect.assert_not_called()
        assert summary.failed_ids == set()
        assert summary.evidence_map == {}
        assert states["h1"].status == CollectionStatus.NOT_STARTED
        run_evidence_collection([hypothesis()], scope, timeout_seconds=1, collection_states=states)
    assert states["h1"].status == CollectionStatus.COMPLETE
    assert states["h1"].attempts == 1


def test_timeout_first_does_not_poison_other_hypotheses_or_one_retry():
    states = {}
    scope = ScopingResult(alarm_summary="incident")
    with patch("rca_agent.services.evidence.collect_evidence") as collect:
        collect.side_effect = [result(failed=True, retryable=True), result("h2"), result()]
        first = run_evidence_collection([hypothesis(), hypothesis("h2")], scope, collection_states=states)
        assert first.failed_ids == {"h1"}
        assert states["h2"].status == CollectionStatus.COMPLETE
        assert collect.call_args_list[0].kwargs["timeout_seconds"] <= 900
        second = run_evidence_collection(
            [hypothesis(), hypothesis("h2")],
            scope,
            collection_states=states,
            existing_evidence_map=first.evidence_map,
        )
        assert second.failed_ids == set()
        assert states["h1"].attempts == 2
        assert states["h1"].status == CollectionStatus.COMPLETE
        assert collect.call_count == 3
        run_evidence_collection([hypothesis()], scope, collection_states=states)
        assert collect.call_count == 3


@pytest.mark.parametrize("finished,retryable,expected_calls", [(False, True, 1), (True, False, 1), (True, True, 2)])
def test_retry_requires_transient_failure_and_request_termination(finished, retryable, expected_calls):
    states = {}
    with patch("rca_agent.services.evidence.collect_evidence") as collect:
        collect.return_value = result(failed=True, retryable=retryable, external_request_finished=finished)
        for _ in range(4):
            run_evidence_collection([hypothesis()], ScopingResult(alarm_summary="x"), collection_states=states)
        assert collect.call_count == expected_calls


def test_permission_error_is_not_retryable():
    agent = MagicMock(side_effect=ClientError({"Error": {"Code": "AccessDeniedException"}}, "Converse"))
    with patch("rca_agent.services.evidence.create_evidence_collection_agent", return_value=agent):
        output = collect_evidence(hypothesis(), ScopingResult(alarm_summary="x"))
    assert output.failed and not output.retryable


def test_async_cancellation_awaits_cleanup_and_rejects_late_result():
    events = []

    class Agent:
        async def invoke_async(self, *args, **kwargs):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                await asyncio.sleep(0.01)
                events.append("external-request-ended")
                return SimpleNamespace(structured_output=EvidenceOutput(combined_summary="late"))

    with (
        patch("rca_agent.utils.agent_invocation._CONTROL_POLL_SECONDS", 0.005),
        invocation_scope(control=stop_after(0.03)),
        pytest.raises(InvocationStoppedError),
    ):
        invoke_agent(Agent(), "prompt", EvidenceOutput, 0.1)
    assert events == ["external-request-ended"]


def test_async_timeout_drains_sdk_executor_before_retry_can_start():
    events = []

    def external_request():
        time.sleep(0.07)
        events.append("request-ended")

    class Agent:
        async def invoke_async(self, *args, **kwargs):
            await asyncio.sleep(0.09)
            await asyncio.to_thread(external_request)

    with (
        patch("rca_agent.utils.agent_invocation._CONTROL_POLL_SECONDS", 0.005),
        invocation_scope(control=stop_after(0.11)),
        pytest.raises(InvocationStoppedError),
    ):
        invoke_agent(Agent(), "prompt", EvidenceOutput, 0.2)
    events.append("retry-allowed")
    assert events == ["request-ended", "retry-allowed"]


def test_lost_attempt_cannot_promote_or_save():
    states = {}

    def lose_ownership(*args, **kwargs):
        states["h1"] = CollectionAttempt(status=CollectionStatus.RUNNING, attempts=2)
        return result()

    with (
        patch("rca_agent.services.evidence.collect_evidence", side_effect=lose_ownership),
        patch("rca_agent.services.evidence._save_single_evidence_to_s3") as save,
    ):
        output = run_evidence_collection(
            [hypothesis()],
            ScopingResult(alarm_summary="x"),
            collection_states=states,
            rca_id="rca",
        )
    save.assert_not_called()
    assert output.evidence_map == {}
    assert states["h1"].status == CollectionStatus.RUNNING


def test_partial_received_tool_results_survive_timeout_as_diagnostics_only():
    class Agent:
        messages = [{"content": [{"toolResult": {"content": [{"text": "received actual observation"}]}}]}]

        async def invoke_async(self, *args, **kwargs):
            await asyncio.sleep(1)

    with (
        patch("rca_agent.services.evidence.create_evidence_collection_agent", return_value=Agent()),
        patch("rca_agent.services.evidence._save_single_evidence_to_s3") as save,
        patch("rca_agent.utils.agent_invocation._CONTROL_POLL_SECONDS", 0.005),
        invocation_scope(control=stop_after(0.03)),
    ):
        output = run_evidence_collection(
            [hypothesis()],
            ScopingResult(alarm_summary="x"),
            timeout_seconds=0.1,
            rca_id="rca",
        )
    assert output.full_evidence_map == {}
    assert output.failed_ids == {"h1"}
    assert "diagnostic" in save.call_args.args[1]
    assert "received actual observation" in save.call_args.args[2]


def test_real_strands_async_invocation_finishes_before_explicit_stop_returns():
    from strands import Agent
    from strands.models.model import Model

    ended = []

    class WaitingModel(Model):
        def update_config(self, **kwargs):
            pass

        def get_config(self):
            return {"model_id": "local-test"}

        async def structured_output(self, *args, **kwargs):
            yield {}

        async def stream(self, *args, **kwargs):
            try:
                yield {"messageStart": {"role": "assistant"}}
                await asyncio.sleep(1)
            finally:
                ended.append("finished")

    agent = Agent(model=WaitingModel(), callback_handler=None)
    with (
        patch("rca_agent.utils.agent_invocation._CONTROL_POLL_SECONDS", 0.005),
        invocation_scope(control=stop_after(0.03)),
        pytest.raises(InvocationStoppedError),
    ):
        invoke_agent(agent, "collect evidence", EvidenceOutput, 0.1)
    assert ended == ["finished"]


def test_completed_cache_is_scoped_and_unknown_request_cannot_restart():
    record = CollectionAttempt(status=CollectionStatus.COMPLETE, scope_key="old")
    assert not collection_is_due(record, "old", "complete evidence")
    assert collection_is_due(record, "new", "complete evidence")
    record.status = CollectionStatus.FAILED
    record.external_request_finished = False
    assert not collection_is_due(record, "new", "failed")


def test_mcp_read_finishes_before_cancelled_invocation_can_retry():
    from mcp.types import Tool

    from rca_agent.adapters.secondary.evidence.readonly_tools import ReadOnlyTool, ReadOnlyTools

    events = []

    class Client:
        async def call_tool_async(self, **kwargs):
            await asyncio.sleep(0.02)
            events.append("request-ended")
            return {"toolUseId": "query", "status": "success", "content": [{"text": "received"}]}

    owner = ReadOnlyTools(Client(), 0.03)
    original = SimpleNamespace(
        mcp_tool=Tool(name="get_metric_data", inputSchema={"type": "object"}),
        mcp_client=owner.client,
        tool_name="get_metric_data",
    )
    tool = ReadOnlyTool(original, owner)

    async def run():
        async def consume():
            async for _ in tool.stream({"toolUseId": "query", "input": {}}, {}):
                events.append("promoted")

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        events.append("retry-allowed")

    asyncio.run(run())
    assert events == ["request-ended", "retry-allowed"]
    assert owner.active == 0
    assert owner.results[0]["content"] == [{"text": "received"}]
