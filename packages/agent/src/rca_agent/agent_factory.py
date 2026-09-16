from __future__ import annotations

import logging
import os
from typing import Any

from botocore.config import Config
from mcp import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from rca_agent.adapters.secondary.evidence.readonly_tools import ReadOnlyTools
from rca_agent.config.settings import (
    BEDROCK_MAX_TOKENS,
    BEDROCK_MODEL_ID,
    BEDROCK_READ_IDLE_SECONDS,
    BEDROCK_REGION,
    GITHUB_PERSONAL_ACCESS_TOKEN,
    LLM_DEFAULT_TIMEOUT_SECONDS,
    THINKING_ENABLED,
)
from rca_agent.prompts.branching import BRANCHING_SYSTEM_PROMPT
from rca_agent.prompts.evidence import EVIDENCE_COLLECTION_SYSTEM_PROMPT
from rca_agent.prompts.hypothesis import HYPOTHESIS_GENERATION_SYSTEM_PROMPT
from rca_agent.prompts.playbook import PLAYBOOK_SYSTEM_PROMPT
from rca_agent.prompts.prioritization import PRIORITIZATION_SYSTEM_PROMPT
from rca_agent.prompts.report import REPORT_SYSTEM_PROMPT
from rca_agent.prompts.scoping import SCOPING_SYSTEM_PROMPT
from rca_agent.prompts.validation import VALIDATION_SYSTEM_PROMPT
from rca_agent.utils.agent_invocation import guard_model_request, own_model_response

logger = logging.getLogger(__name__)


class _ExplicitThinkingBedrockModel(BedrockModel):
    """Preserve explicit disabled thinking on forced-tool packets without forking the SDK."""

    def _get_additional_request_fields(self, tool_choice):
        fields = super()._get_additional_request_fields(tool_choice)
        configured = self.config.get("additional_request_fields", {})
        if configured.get("thinking") == {"type": "disabled"}:
            fields = {
                **fields,
                "additionalModelRequestFields": {
                    **fields.get("additionalModelRequestFields", {}),
                    "thinking": {"type": "disabled"},
                },
            }
        return fields


def _own_streaming_client(model):
    """Register narrow SDK packet/response hooks; each call uses its current invocation owner."""
    events = model.client.meta.events
    events.register("before-parameter-build.bedrock-runtime.ConverseStream", guard_model_request)
    events.register("after-call.bedrock-runtime.ConverseStream", own_model_response)
    return model


def _build_thinking_fields() -> dict[str, Any]:
    """Adaptive thinking only — the model tier decision forbids an effort level.

    The target Bedrock deployment rejects `effort`, so Planning and Execution are
    distinguished by whether adaptive thinking is on at all.
    """
    if THINKING_ENABLED:
        return {"thinking": {"type": "adaptive"}}
    return {"thinking": {"type": "disabled"}}


def create_planning_model(
    *,
    model_id: str = BEDROCK_MODEL_ID,
    region: str = BEDROCK_REGION,
    max_tokens: int = BEDROCK_MAX_TOKENS,
    request_timeout_seconds: float = BEDROCK_READ_IDLE_SECONDS,
) -> BedrockModel:
    """Stream reasoning with an idle socket limit, independent of the work-start budget."""
    additional = _build_thinking_fields()
    # No sampling parameters: this model generation rejects `temperature`.
    return _own_streaming_client(
        _ExplicitThinkingBedrockModel(
            model_id=model_id,
            region_name=region,
            max_tokens=max_tokens,
            streaming=True,
            cache_prompt="default",
            boto_client_config=Config(
                tcp_keepalive=True,
                connect_timeout=min(5, request_timeout_seconds / 4),
                read_timeout=request_timeout_seconds,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
            additional_request_fields=additional,
        )
    )


def create_execution_model(
    *,
    model_id: str = BEDROCK_MODEL_ID,
    region: str = BEDROCK_REGION,
    max_tokens: int = BEDROCK_MAX_TOKENS,
    request_timeout_seconds: float = BEDROCK_READ_IDLE_SECONDS,
) -> BedrockModel:
    """Stream execution responses with explicit disabled thinking and socket idle timeout."""
    return _own_streaming_client(
        _ExplicitThinkingBedrockModel(
            model_id=model_id,
            region_name=region,
            max_tokens=max_tokens,
            streaming=True,
            cache_prompt="default",
            additional_request_fields={"thinking": {"type": "disabled"}},
            boto_client_config=Config(
                tcp_keepalive=True,
                connect_timeout=min(2, request_timeout_seconds / 4),
                read_timeout=request_timeout_seconds,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )
    )


def _mcp_env(**extra: str) -> dict[str, str]:
    env = {**os.environ, "FASTMCP_LOG_LEVEL": "ERROR"}
    env.update(extra)
    return env


def create_cloudwatch_mcp_client() -> MCPClient:
    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command="uvx",
                args=["--from", "awslabs-cloudwatch-mcp-server", "awslabs.cloudwatch-mcp-server"],
                env=_mcp_env(),
            )
        )
    )


def create_cloudtrail_mcp_client() -> MCPClient:
    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command="uvx",
                args=["--from", "awslabs-cloudtrail-mcp-server", "awslabs.cloudtrail-mcp-server"],
                env=_mcp_env(),
            )
        )
    )


AWS_KNOWLEDGE_MCP_URL = "https://knowledge-mcp.global.api.aws"


def create_aws_knowledge_mcp_client() -> MCPClient:
    return MCPClient(lambda: streamablehttp_client(AWS_KNOWLEDGE_MCP_URL))


def create_github_mcp_client() -> MCPClient:
    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command="github-mcp-server",
                args=["stdio"],
                env=_mcp_env(
                    GITHUB_PERSONAL_ACCESS_TOKEN=GITHUB_PERSONAL_ACCESS_TOKEN,
                    GITHUB_READ_ONLY="1",
                    GITHUB_TOOLSETS="repos,pull_requests",
                ),
            )
        )
    )


def create_scoping_agent(
    *,
    model: BedrockModel | None = None,
    mcp_clients: list[MCPClient] | None = None,
) -> Agent:
    if model is None:
        model = create_execution_model()

    tools: list = []
    if mcp_clients:
        tools.extend(ReadOnlyTools(client, 60) for client in mcp_clients)

    agent = Agent(
        model=model,
        system_prompt=SCOPING_SYSTEM_PROMPT,
        tools=tools,
    )
    agent._rca_read_tools = tools
    return agent


def create_hypothesis_generation_agent(
    *,
    model: BedrockModel | None = None,
) -> Agent:
    if model is None:
        model = create_planning_model()

    return Agent(
        model=model,
        system_prompt=HYPOTHESIS_GENERATION_SYSTEM_PROMPT,
    )


def create_prioritization_agent(*, model: BedrockModel | None = None) -> Agent:
    if model is None:
        model = create_planning_model()
    return Agent(model=model, system_prompt=PRIORITIZATION_SYSTEM_PROMPT)


def create_evidence_collection_agent(
    *,
    model: BedrockModel | None = None,
    mcp_clients: list[MCPClient] | None = None,
    invocation_timeout_seconds: float = LLM_DEFAULT_TIMEOUT_SECONDS,
) -> Agent:
    """Keep the legacy budget argument; invoke_agent owns admission and the caller owns the batch clock."""
    if model is None:
        model = create_execution_model()

    tools: list = []
    if mcp_clients:
        tools.extend(ReadOnlyTools(client, 60) for client in mcp_clients)

    agent = Agent(
        model=model,
        system_prompt=EVIDENCE_COLLECTION_SYSTEM_PROMPT,
        tools=tools,
    )
    agent._rca_read_tools = tools
    return agent


def create_validation_agent(*, model: BedrockModel | None = None) -> Agent:
    if model is None:
        model = create_execution_model()
    return Agent(model=model, system_prompt=VALIDATION_SYSTEM_PROMPT)


def create_branching_agent(*, model: BedrockModel | None = None) -> Agent:
    if model is None:
        model = create_planning_model()
    return Agent(model=model, system_prompt=BRANCHING_SYSTEM_PROMPT)


def create_report_agent(*, model: BedrockModel | None = None) -> Agent:
    if model is None:
        model = create_planning_model()
    return Agent(model=model, system_prompt=REPORT_SYSTEM_PROMPT)


def create_playbook_agent(*, model: BedrockModel | None = None) -> Agent:
    if model is None:
        model = create_planning_model()
    return Agent(model=model, system_prompt=PLAYBOOK_SYSTEM_PROMPT)
