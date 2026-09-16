"""Actual Strands/botocore request lifecycle with local event streams and no AWS access."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from botocore.exceptions import ReadTimeoutError
from pydantic import BaseModel
from strands import Agent

from rca_agent.agent_factory import create_execution_model
from rca_agent.utils import agent_invocation as invocation


class Output(BaseModel):
    value: str


class LocalStream:
    """Represent transport data, silence and close acknowledgement independently of stage time."""

    def __init__(self, events, delay=0.02, idle_error=False, hold=False):
        self.events, self.delay, self.idle_error, self.hold = events, delay, idle_error, hold
        self.closed = threading.Event()
        self.started = threading.Event()
        self.ended = threading.Event()
        self.worker = None

    def __iter__(self):
        self.worker = threading.current_thread()
        self.started.set()
        try:
            if self.hold:
                assert self.closed.wait(5), "test cancellation did not close the actual stream"
                return
            for event in self.events:
                if self.closed.wait(self.delay):
                    return
                yield event
            if self.idle_error:
                raise ReadTimeoutError(endpoint_url="http://local-test-only")
        finally:
            self.ended.set()

    def close(self):
        self.closed.set()


def text_events():
    """Use the actual ConverseStream event shape consumed by Strands."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
    ]


def local_agent(monkeypatch, *, delay=0.02, idle_error=False, hold=False, structured=False):
    """Keep botocore serialization/event hooks; replace only the network transport boundary."""
    model = create_execution_model()
    packets, streams = [], []

    def transport(operation, request, context):
        assert operation.name == "ConverseStream"
        packet = json.loads(request["body"])
        packets.append(packet)
        events = text_events()
        if structured:
            tool = next(t["toolSpec"]["name"] for t in packet["toolConfig"]["tools"] if "toolSpec" in t)
            events = [
                {"messageStart": {"role": "assistant"}},
                {
                    "contentBlockStart": {
                        "contentBlockIndex": 0,
                        "start": {"toolUse": {"toolUseId": "one", "name": tool}},
                    }
                },
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"value":"ok"}'}}}},
                {"contentBlockStop": {"contentBlockIndex": 0}},
                {"messageStop": {"stopReason": "tool_use"}},
                {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
            ]
        stream = LocalStream(events, delay, idle_error, hold)
        streams.append(stream)
        return SimpleNamespace(status_code=200, headers={}), {"stream": stream}

    monkeypatch.setattr(model.client, "_make_request", transport)
    agent = Agent(model=model, system_prompt="Local transport test", callback_handler=None)
    return agent, packets, streams


def test_response_and_local_structured_validation_survive_expired_stage_and_overall_budget(monkeypatch):
    """A real Strands structured response continues and validates after both admission budgets expire."""
    agent, packets, streams = local_agent(monkeypatch, delay=0.04, structured=True)
    progress = []
    deadline = time.monotonic() + 0.08
    with invocation.invocation_scope(admission_deadline=deadline, on_progress=progress.append):
        result = invocation.invoke_agent(agent, "return ok", Output, 0.1)
    assert result.value == "ok"
    assert time.monotonic() > deadline
    assert len(packets) == 1 and packets[0]["inferenceConfig"]["maxTokens"] == 65536
    assert packets[0]["additionalModelRequestFields"]["thinking"] == {"type": "disabled"}
    assert streams[0].ended.is_set() and streams[0].closed.is_set()
    assert not streams[0].worker.is_alive()
    assert progress


def test_new_model_call_after_expiry_never_reaches_transport(monkeypatch):
    """A second request cannot use a completed first response to restart the admission clock."""
    agent, packets, streams = local_agent(monkeypatch, delay=0.03)
    with invocation.invocation_scope(admission_deadline=time.monotonic() + 0.05):
        invocation.invoke_agent(agent, "one", None, 1)
        with pytest.raises(invocation.InvocationNotStartedError):
            invocation.invoke_agent(agent, "two", None, 1)
    assert len(packets) == 1 and streams[0].ended.is_set()


def test_internal_model_retry_is_denied_after_active_response_uses_budget(monkeypatch):
    """A non-structured response may finish late, but a new forced-output retry is not admitted."""
    agent, packets, streams = local_agent(monkeypatch, delay=0.03)
    with pytest.raises(Exception, match="budget exhausted"):
        invocation.invoke_agent(agent, "return Output", Output, 0.06)
    assert len(packets) == 1
    assert streams[0].ended.is_set() and not streams[0].worker.is_alive()


@pytest.mark.parametrize("reason", ["cancelled", "claim lost"])
def test_explicit_control_closes_transport_and_never_returns_late_output(monkeypatch, reason):
    """Cancellation/ownership loss closes the actual response and joins its worker before returning."""
    agent, packets, streams = local_agent(monkeypatch, hold=True)
    monkeypatch.setattr(invocation, "_CONTROL_POLL_SECONDS", 0.005)

    def control():
        if streams and streams[0].started.is_set():
            raise RuntimeError(reason)

    with invocation.invocation_scope(control=control), pytest.raises(RuntimeError, match=reason):
        invocation.invoke_agent(agent, "hold", None, 1)
    assert len(packets) == 1
    assert streams[0].closed.is_set() and streams[0].ended.is_set()
    assert not streams[0].worker.is_alive()


def test_control_rechecked_before_return_even_for_an_immediate_result():
    """A completed coroutine cannot publish after ownership was lost during the request."""
    lost = False

    class Immediate:
        async def invoke_async(self, *args, **kwargs):
            nonlocal lost
            lost = True
            return SimpleNamespace(structured_output="late")

    with invocation.invocation_scope(control=lambda: not lost), pytest.raises(invocation.InvocationStoppedError):
        invocation.invoke_agent(Immediate(), "x", None, 1)


def test_socket_idle_error_finishes_before_retry_without_worker_overlap(monkeypatch):
    """Transport read failure remains a failure even when the stage still has time."""
    agent, packets, streams = local_agent(monkeypatch, idle_error=True)
    for _ in range(2):
        with pytest.raises(Exception, match="Read timeout"):
            invocation.invoke_agent(agent, "read", None, 1)
        assert all(s.ended.is_set() and s.closed.is_set() and not s.worker.is_alive() for s in streams)
    assert len(packets) == 2


def test_expired_admission_does_not_start_or_consume_an_attempt():
    """NOT_STARTED is about expired admission, not a fabricated response-duration reserve."""
    starts = []

    class AgentDouble:
        async def invoke_async(self, *args, **kwargs):
            pytest.fail("not admitted")

    with (
        invocation.invocation_scope(admission_deadline=time.monotonic() - 1),
        pytest.raises(invocation.InvocationNotStartedError),
    ):
        invocation.invoke_agent(AgentDouble(), "x", None, 900, on_started=lambda: starts.append(1))
    assert starts == []


def test_positive_short_budget_can_admit_a_response_longer_than_that_budget():
    """The former 40 ms/300 ms example is now a legitimate admitted response, not a timeout defect."""

    class AgentDouble:
        async def invoke_async(self, *args, **kwargs):
            await asyncio.sleep(0.08)
            return SimpleNamespace(structured_output="complete")

    assert invocation.invoke_agent(AgentDouble(), "x", None, 0.04) == "complete"


def test_mcp_admission_refuses_late_new_query_but_retains_received_results(monkeypatch):
    """Actual readonly tool views gate new external requests, not local receipt preservation."""
    from mcp.types import Tool

    from rca_agent.adapters.secondary.evidence.readonly_tools import ReadOnlyTool, ReadOnlyTools

    calls = []

    class Client:
        async def call_tool_async(self, **kwargs):
            calls.append(kwargs)
            await asyncio.sleep(0.05)
            return {"toolUseId": "one", "status": "success", "content": [{"text": "observed"}]}

    owner = ReadOnlyTools(Client(), 60)
    tool = ReadOnlyTool(
        SimpleNamespace(
            mcp_tool=Tool(name="get_metric_data", inputSchema={"type": "object"}),
            mcp_client=owner.client,
            tool_name="get_metric_data",
        ),
        owner,
    )

    class AgentDouble:
        async def invoke_async(self, *args, **kwargs):
            async for _ in tool.stream({"toolUseId": "one", "input": {}}, {}):
                pass
            with pytest.raises(invocation.WorkAdmissionError):
                async for _ in tool.stream({"toolUseId": "two", "input": {}}, {}):
                    pytest.fail("late external request delivered")
            return SimpleNamespace(structured_output="first observation")

    assert invocation.invoke_agent(AgentDouble(), "x", None, 0.02) == "first observation"
    assert len(calls) == len(owner.receipts) == 1 and owner.active == 0


def test_sync_nonmodel_adapter_keeps_its_existing_hard_timeout():
    def adapter(*args, **kwargs):
        time.sleep(0.2)
        return SimpleNamespace(structured_output="late")

    with pytest.raises(TimeoutError):
        invocation.invoke_agent(adapter, "x", None, 0.02)
