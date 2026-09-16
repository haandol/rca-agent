"""Use real SDK agents with an offline model to test incident boundaries, not each invocation."""

import asyncio
import json
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from strands import Agent
from strands.models.model import Model

from rca_agent.adapters.secondary.evidence.readonly_tools import ReadOnlyTools
from rca_agent.di.app_container import AppContainer
from rca_agent.services.pipeline import PipelineOrchestrator


class RecordingModel(Model):
    """Record the actual messages passed by the SDK without constructing an AWS client."""

    def __init__(self):
        self.inputs = []

    def get_config(self):
        return {}

    def update_config(self, **kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("This test uses the SDK conversation stream")
        yield

    async def stream(self, messages, *args, **kwargs):
        self.inputs.append(deepcopy(messages))
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "observed"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}}


STAGES = {
    "scoping_agent": "create_scoping_agent",
    "hypothesis_agent": "create_hypothesis_generation_agent",
    "prioritization_agent": "create_prioritization_agent",
    "validation_agent": "create_validation_agent",
    "branching_agent": "create_branching_agent",
    "report_agent": "create_report_agent",
    "playbook_agent": "create_playbook_agent",
}


@pytest.fixture
def setup():
    shared_client = MagicMock(name="shared-mcp-connection")
    container = AppContainer("offline")
    container._scoping_mcp_clients = [shared_client]
    container._evidence_mcp_clients = [shared_client]
    models = {name: RecordingModel() for name in STAGES}
    agents = {name: Agent(model=model, callback_handler=None) for name, model in models.items()}
    provider = ReadOnlyTools(shared_client, 60)
    agents["scoping_agent"]._rca_read_tools = [provider]
    with ExitStack() as patches:
        for name, factory in STAGES.items():
            patches.enter_context(patch("rca_agent.agent_factory." + factory, return_value=agents[name]))
            assert getattr(container, name) is agents[name]
        yield container, agents, models, provider, shared_client


def test_two_successive_pipeline_incidents_do_not_inherit_conversation_or_tool_taint(setup):
    """Second RCA receives only its prompt and explicitly retrieved knowledge; same-RCA history survives."""
    container, agents, models, provider, shared = setup
    saved_history = []
    unrelated = Agent(
        model=RecordingModel(), messages=[{"role": "user", "content": [{"text": "OTHER_AGENT"}]}], callback_handler=None
    )
    ports = [MagicMock() for _ in range(3)]
    container._report_store, container._playbook_store, container._session_store = ports
    pipeline = PipelineOrchestrator(container)

    def stages(alarm, run):
        marker = run.rca_id
        assert provider.active == 0 and not provider.termination_uncertain
        assert provider.receipts == [] and provider.results == []
        for name, agent in agents.items():
            assert agent.messages == [] and agent.state.get() == {}
            assert agent.conversation_manager.removed_message_count == 0
            assert agent._model_state == {}
            asyncio.run(agent.invoke_async(marker + " EXPLICIT_LIBRARY_KNOWLEDGE"))
            asyncio.run(agent.invoke_async("same incident followup"))
            assert marker in json.dumps(models[name].inputs[-1])
            if marker == "INCIDENT_B":
                assert "INCIDENT_A" not in json.dumps(models[name].inputs[-2:])
            agent.state.set("incident", marker)
            agent._model_state["conversation_id"] = marker
            agent.conversation_manager.removed_message_count = 7
        provider.record({"status": "error", "marker": marker}, "get_log_events", {"incident": marker})
        saved_history.append((deepcopy(agents["scoping_agent"].messages), provider.receipts))
        return True

    with patch.object(pipeline, "_run_pipeline_in_context", side_effect=stages):
        assert pipeline._run_pipeline(None, SimpleNamespace(rca_id="INCIDENT_A"))
        assert pipeline._run_pipeline(None, SimpleNamespace(rca_id="INCIDENT_B"))
    assert "INCIDENT_A" in json.dumps(saved_history[0])
    assert "INCIDENT_B" in json.dumps(saved_history[1])
    for name, agent in agents.items():
        assert getattr(container, name) is agent and agent.model is models[name]
    assert provider.client is shared and container.scoping_mcp_clients[0] is shared
    assert container.evidence_mcp_clients[0] is shared
    shared.close.assert_not_called()
    shared.remove_consumer.assert_not_called()
    assert [container._report_store, container._playbook_store, container._session_store] == ports
    assert "OTHER_AGENT" in str(unrelated.messages)


@pytest.mark.parametrize("active", ["analysis", "provider", "agent"])
def test_reset_refuses_active_work_before_mutating_any_stage(setup, active):
    """All stages and received data survive a rejected reset; locks release for a later valid RCA."""
    container, agents, _, provider, _ = setup
    marker = {"role": "user", "content": [{"text": "ACTIVE_MARKER"}]}
    agents["hypothesis_agent"].messages.append(deepcopy(marker))
    provider.record({"marker": "RECEIVED"}, "get_log_events", {})
    receipts = deepcopy(provider.receipts)
    if active == "provider":
        provider.active = 1
        with pytest.raises(RuntimeError, match="active tool provider"), container.analysis_context():
            pass
        provider.active = 0
    elif active == "agent":
        assert agents["report_agent"]._concurrency.try_acquire_lock()
        try:
            with pytest.raises(RuntimeError, match="actively invoked"), container.analysis_context():
                pass
        finally:
            agents["report_agent"]._concurrency.release_lock()
    else:
        with container.analysis_context():
            agents["hypothesis_agent"].messages.append(deepcopy(marker))
            provider.record({"marker": "RECEIVED"}, "get_log_events", {})
            with pytest.raises(RuntimeError, match="active analysis"), container.analysis_context():
                pass
    assert agents["hypothesis_agent"].messages == [marker]
    assert provider.receipts == receipts
    with container.analysis_context():
        assert agents["hypothesis_agent"].messages == []
        assert provider.receipts == []


def test_empty_container_scope_keeps_agent_and_connection_creation_lazy():
    container = AppContainer("offline")
    with container.analysis_context():
        assert container._scoping_agent is None and container._scoping_mcp_clients is None
    assert container._hypothesis_agent is None
