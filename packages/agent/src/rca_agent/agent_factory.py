from __future__ import annotations

import logging
from typing import Any

from mcp import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from rca_agent.config import (
    BEDROCK_HAIKU_MAX_TOKENS,
    BEDROCK_HAIKU_MODEL_ID,
    BEDROCK_MAX_TOKENS,
    BEDROCK_MODEL_ID,
    BEDROCK_REGION,
    GITHUB_PERSONAL_ACCESS_TOKEN,
    THINKING_ENABLED,
)
from rca_agent.prompts import (
    BRANCHING_SYSTEM_PROMPT,
    EVIDENCE_COLLECTION_SYSTEM_PROMPT,
    HYPOTHESIS_GENERATION_SYSTEM_PROMPT,
    PLAYBOOK_SYSTEM_PROMPT,
    PRIORITIZATION_SYSTEM_PROMPT,
    REPORT_SYSTEM_PROMPT,
    SCOPING_SYSTEM_PROMPT,
    VALIDATION_SYSTEM_PROMPT,
    VERIFICATION_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


def _build_thinking_fields() -> dict[str, Any]:
    if THINKING_ENABLED:
        return {"thinking": {"type": "adaptive"}}
    return {}


def create_planning_model(
    *,
    model_id: str = BEDROCK_MODEL_ID,
    region: str = BEDROCK_REGION,
    max_tokens: int = BEDROCK_MAX_TOKENS,
) -> BedrockModel:
    additional = _build_thinking_fields()
    return BedrockModel(
        model_id=model_id,
        region_name=region,
        max_tokens=max_tokens,
        temperature=0.3,
        streaming=False,
        **({"additional_request_fields": additional} if additional else {}),
    )


def create_execution_model(
    *,
    model_id: str = BEDROCK_HAIKU_MODEL_ID,
    region: str = BEDROCK_REGION,
    max_tokens: int = BEDROCK_HAIKU_MAX_TOKENS,
) -> BedrockModel:
    return BedrockModel(
        model_id=model_id,
        region_name=region,
        max_tokens=max_tokens,
        temperature=0.3,
        streaming=False,
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


def create_cloudtrail_mcp_client() -> MCPClient:
    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command="uvx",
                args=["awslabs.cloudtrail-mcp-server@latest"],
                env={"FASTMCP_LOG_LEVEL": "ERROR"},
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
                command="docker",
                args=[
                    "run",
                    "-i",
                    "--rm",
                    "-e",
                    "GITHUB_PERSONAL_ACCESS_TOKEN",
                    "-e",
                    "GITHUB_TOOLSETS=repos,pull_requests",
                    "ghcr.io/github/github-mcp-server",
                ],
                env={
                    "GITHUB_PERSONAL_ACCESS_TOKEN": GITHUB_PERSONAL_ACCESS_TOKEN,
                },
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
) -> Agent:
    if model is None:
        model = create_execution_model()

    tools: list = []
    if mcp_clients:
        tools.extend(mcp_clients)

    return Agent(
        model=model,
        system_prompt=EVIDENCE_COLLECTION_SYSTEM_PROMPT,
        tools=tools,
    )


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


def create_verification_agent(
    *,
    model: BedrockModel | None = None,
    mcp_clients: list[MCPClient] | None = None,
) -> Agent:
    if model is None:
        model = create_execution_model()

    tools: list = []
    if mcp_clients:
        tools.extend(mcp_clients)

    return Agent(
        model=model,
        system_prompt=VERIFICATION_SYSTEM_PROMPT,
        tools=tools,
    )
