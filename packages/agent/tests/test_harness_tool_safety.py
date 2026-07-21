from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rca_agent.agent_factory import (
    create_branching_agent,
    create_evidence_collection_agent,
    create_github_mcp_client,
    create_hypothesis_generation_agent,
    create_playbook_agent,
    create_prioritization_agent,
    create_report_agent,
    create_scoping_agent,
    create_validation_agent,
)

PLANNING_AND_VALIDATION_FACTORIES = [
    create_hypothesis_generation_agent,
    create_prioritization_agent,
    create_validation_agent,
    create_branching_agent,
    create_report_agent,
    create_playbook_agent,
]


@pytest.mark.parametrize("factory", PLANNING_AND_VALIDATION_FACTORIES)
def test_non_evidence_agents_are_created_without_tool_access(factory):
    model = object()
    with patch("rca_agent.agent_factory.Agent") as agent_class:
        factory(model=model)

    kwargs = agent_class.call_args.kwargs
    assert kwargs["model"] is model
    assert "tools" not in kwargs or kwargs["tools"] == []


@pytest.mark.parametrize(
    "factory",
    [create_scoping_agent, create_evidence_collection_agent],
)
def test_tool_enabled_agents_expose_only_explicitly_supplied_clients(factory):
    model = object()
    approved_clients = [object(), object()]
    with patch("rca_agent.agent_factory.Agent") as agent_class:
        factory(model=model, mcp_clients=approved_clients)

    assert agent_class.call_args.kwargs["tools"] == approved_clients


@pytest.mark.parametrize(
    "factory",
    [create_scoping_agent, create_evidence_collection_agent],
)
def test_tool_enabled_agents_default_to_no_tools(factory):
    with patch("rca_agent.agent_factory.Agent") as agent_class:
        factory(model=object())

    assert agent_class.call_args.kwargs["tools"] == []


def test_github_mcp_transport_enforces_read_only_mode():
    captured_transport = None

    def capture_transport(transport):
        nonlocal captured_transport
        captured_transport = transport
        return MagicMock()

    with patch("rca_agent.agent_factory.MCPClient", side_effect=capture_transport):
        create_github_mcp_client()

    assert captured_transport is not None
    with patch("rca_agent.agent_factory.stdio_client") as stdio_client:
        captured_transport()

    params = stdio_client.call_args.args[0]
    assert params.env["GITHUB_READ_ONLY"] == "1"
    assert params.env["GITHUB_TOOLSETS"] == "repos,pull_requests"
