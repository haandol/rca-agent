from __future__ import annotations

import logging

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from rca_agent.config import BEDROCK_MAX_TOKENS, BEDROCK_MODEL_ID, BEDROCK_REGION
from rca_agent.prompts import HYPOTHESIS_GENERATION_SYSTEM_PROMPT, SCOPING_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def create_bedrock_model(
    *,
    model_id: str = BEDROCK_MODEL_ID,
    region: str = BEDROCK_REGION,
    max_tokens: int = BEDROCK_MAX_TOKENS,
) -> BedrockModel:
    return BedrockModel(
        model_id=model_id,
        region_name=region,
        max_tokens=max_tokens,
        temperature=0.3,
    )


def create_cloudwatch_mcp_client() -> MCPClient:
    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command="uvx",
                args=["awslabs.cw-mcp-server@latest"],
                env={"FASTMCP_LOG_LEVEL": "ERROR"},
            )
        )
    )


def create_scoping_agent(
    *,
    model: BedrockModel | None = None,
    mcp_clients: list[MCPClient] | None = None,
) -> Agent:
    """Create a Strands Agent configured for the scoping phase.

    The agent is wired with the CloudWatch MCP server tools for metric queries.
    """
    if model is None:
        model = create_bedrock_model()

    tools: list = []
    if mcp_clients:
        tools.extend(mcp_clients)

    return Agent(
        model=model,
        system_prompt=SCOPING_SYSTEM_PROMPT,
        tools=tools,
    )


def create_hypothesis_generation_agent(
    *,
    model: BedrockModel | None = None,
) -> Agent:
    if model is None:
        model = create_bedrock_model()

    return Agent(
        model=model,
        system_prompt=HYPOTHESIS_GENERATION_SYSTEM_PROMPT,
    )
