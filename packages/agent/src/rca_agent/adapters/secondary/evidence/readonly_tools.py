"""Per-invocation tool views share connections without sharing request ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from urllib.parse import quote

from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent

from rca_agent.utils.agent_invocation import require_request_budget

# Query execution is read-only even though CloudWatch names it "execute".
_READ_QUERIES = {"execute_log_insights_query", "get_logs_insight_query_results", "lookup_events"}
_READ_PREFIXES = ("get_", "list_", "describe_", "search_", "read_", "batch_get_")


class ReadOnlyTool(MCPAgentTool):
    def __init__(self, original, owner):
        """Keep the shared transport but give each invocation its own timeout and result owner."""
        super().__init__(
            original.mcp_tool,
            original.mcp_client,
            name_override=original.tool_name,
            timeout=timedelta(seconds=owner.request_timeout),
        )
        self.owner = owner

    async def stream(self, tool_use, invocation_state, **kwargs):
        """Drain the exact read request; a cancelled invocation never receives its late result."""
        require_request_budget()
        request = asyncio.create_task(
            self.mcp_client.call_tool_async(
                tool_use_id=tool_use["toolUseId"],
                name=self.mcp_tool.name,
                arguments=tool_use["input"],
                read_timeout_seconds=self.timeout,
            )
        )
        self.owner.active += 1
        try:
            try:
                result = await asyncio.shield(request)
            except asyncio.CancelledError:
                # MCP cancellation notifications do not acknowledge remote termination.
                # Wait for this bounded request instead of treating notification as proof.
                try:
                    result = await request
                    self.owner.record(
                        result, self.mcp_tool.name, tool_use["input"], expected_tool_use_id=tool_use["toolUseId"]
                    )
                except BaseException as exc:
                    self.owner.record_transport_failure(self.mcp_tool.name, tool_use, exc)
                raise
            self.owner.record(result, self.mcp_tool.name, tool_use["input"], expected_tool_use_id=tool_use["toolUseId"])
            yield ToolResultEvent(result)
        except Exception as exc:
            self.owner.record_transport_failure(self.mcp_tool.name, tool_use, exc)
            raise
        finally:
            self.owner.active -= 1


class ReadOnlyTools(ToolProvider):
    def __init__(self, client, request_timeout: float):
        """Track request termination separately from the lifetime of a shared MCP connection."""
        self.client = client
        self.request_timeout = max(0.001, request_timeout)
        self.active = 0
        self.termination_uncertain = False
        self.results: list[dict] = []
        self.receipts: list[dict] = []

    def record(self, result, tool_name=None, arguments=None, *, expected_tool_use_id=None):
        """A correlated MCP error result ends its RPC; SDK-generated transport errors may not.

        The installed SDK includes isError on returned MCP CallToolResult objects,
        but omits it when converting a transport exception to status=error. Never
        infer request termination from error prose or an HTTP status mentioned in it.
        """
        received = deepcopy(result)
        self.results.append(received)
        request_id = received.get("toolUseId") if isinstance(received, dict) else None
        correlated = (
            isinstance(request_id, str)
            and bool(request_id)
            and (expected_tool_use_id is None or request_id == expected_tool_use_id)
        )
        error = isinstance(received, dict) and (received.get("status") == "error" or received.get("isError") is True)
        unknown = (
            not isinstance(received, dict)
            or received.get("status") not in {"success", "error"}
            or bool(received.get("cancelled"))
            or (expected_tool_use_id is not None and not correlated)
            or (error and not (correlated and received.get("isError") is True))
        )
        if unknown:
            self.termination_uncertain = True
        if tool_name is not None:
            digest = hashlib.sha256(json.dumps(received, sort_keys=True, default=str).encode()).hexdigest()
            source = (
                f"mcp-tool-result://{quote(str(tool_name), safe='')}/"
                f"{quote(str(request_id or 'unidentified'), safe='')}#sha256={digest}"
            )
            self.receipts.append(
                {
                    "tool_name": tool_name,
                    "arguments": deepcopy(arguments),
                    "result": received,
                    "request_terminated": not unknown,
                    "requested_tool_use_id": expected_tool_use_id,
                    "source_ref": source,
                }
            )

    def record_transport_failure(self, tool_name, tool_use, exc):
        """Keep an unacknowledged transport failure explicit; never manufacture a completed RPC."""
        self.record(
            {
                "status": "error",
                "toolUseId": tool_use["toolUseId"],
                "transport_error_type": type(exc).__name__,
                "content": [{"text": str(exc) or type(exc).__name__}],
                **({"cancelled": True} if isinstance(exc, asyncio.CancelledError) else {}),
            },
            tool_name,
            tool_use["input"],
            expected_tool_use_id=tool_use["toolUseId"],
        )

    async def load_tools(self, **kwargs):
        """Expose known reads only, irrespective of a remote annotation claiming write safety."""
        require_request_budget()
        tools = await self.client.load_tools(**kwargs)
        return [
            ReadOnlyTool(tool, self)
            for tool in tools
            if tool.mcp_tool.name in _READ_QUERIES or tool.mcp_tool.name.startswith(_READ_PREFIXES)
        ]

    def add_consumer(self, consumer_id, **kwargs):
        """Let the shared client retain its established connection ownership contract."""
        self.client.add_consumer(consumer_id, **kwargs)

    def remove_consumer(self, consumer_id, **kwargs):
        """Release only this consumer so other hypothesis invocations keep their connection."""
        self.client.remove_consumer(consumer_id, **kwargs)
