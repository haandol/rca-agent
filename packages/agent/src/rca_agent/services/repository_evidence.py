"""Deliberately read bounded immutable source and CI through the existing readonly MCP port."""

from __future__ import annotations

import asyncio
import uuid

from rca_agent.adapters.secondary.evidence.readonly_tools import ReadOnlyTools
from rca_agent.services.collected_observations import _objects
from rca_agent.services.frozen_evidence import (
    frozen_evidence_scope,
    received_control_artifacts,
    received_source_artifacts,
)
from rca_agent.utils.agent_invocation import InvocationStoppedError, require_request_budget
from rca_agent.utils.source_locations import declared_source_locations


def source_locations(scoping) -> list[dict]:
    """Use declared immutable locators only as candidates for independently verified Git reads."""
    if scoping.raw_alarm and scoping.raw_alarm.eval_source_metadata is not None:
        return []
    locations = []
    for phase in ("current", "baseline"):
        for event in getattr(scoping.incident_observations, phase).get("observations", []):
            for location in declared_source_locations(event.get("message", {})).values():
                if location not in locations:
                    locations.append(location)
    return locations


async def _owned(awaitable, control):
    """Drain a bounded MCP operation on cancellation and reject its result after ownership loss."""
    task = asyncio.create_task(awaitable)
    try:
        while not task.done():
            if control() is False:
                raise InvocationStoppedError("repository read ownership lost")
            await asyncio.wait({task}, timeout=0.1)
        if control() is False:
            raise InvocationStoppedError("repository read ownership lost")
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def collect_repository_evidence(scoping, client, *, controls=False, prior_receipts=(), control=lambda: None) -> dict:
    """Read at most four files/discovery results under caller admission; never infer missing controls."""
    result = {"source_artifacts": [], "control_artifacts": [], "receipts": [], "warnings": []}
    locations = source_locations(scoping)
    if not locations:
        result["warnings"].append("immutable repository locator unavailable")
        return result
    if client is None:
        result["warnings"].append("readonly GitHub provider unavailable")
        return result
    with frozen_evidence_scope(scoping):
        provider = ReadOnlyTools(client, 60)
    consumer = "repository-evidence-" + uuid.uuid4().hex

    async def collect():
        """Load one scoped read provider and execute actual tool streams without a model."""
        require_request_budget()
        tools = await _owned(provider.load_tools(), control)
        read = next((tool for tool in tools if tool.mcp_tool.name == "get_file_contents"), None)
        if read is None:
            result["warnings"].append("immutable file read tool unavailable")
            return
        calls = 0

        async def fetch(repository, commit, path):
            """Collect one correlated immutable file response through the existing receipt owner."""
            nonlocal calls
            if calls >= 4 or provider.termination_uncertain:
                return False
            require_request_budget()
            if control() is False:
                raise InvocationStoppedError("repository read ownership lost")
            owner, repo = repository.split("/", 1)
            calls += 1
            request = {
                "toolUseId": uuid.uuid4().hex,
                "name": "get_file_contents",
                "input": {"owner": owner, "repo": repo, "ref": commit, "path": path},
            }

            async def receive():
                """Drain the readonly tool generator so its actual response is recorded."""
                async for _ in read.stream(request, {}):
                    pass

            try:
                await _owned(receive(), control)
            except InvocationStoppedError:
                raise
            except Exception as exc:
                # A real control/claim exception must not be converted to optional evidence.
                control()
                result["warnings"].append(f"repository read failed: {type(exc).__name__}")
                return False
            receipt = provider.receipts[-1] if provider.receipts else {}
            reply = receipt.get("result", {})
            success = (
                receipt.get("request_terminated") is True
                and reply.get("status") == "success"
                and not reply.get("isError")
            )
            if not success:
                result["warnings"].append("repository read unavailable; not evidence of file absence")
            return success

        verified = received_source_artifacts(list(prior_receipts), scoping)
        for location in locations:
            if controls and verified:
                break
            await fetch(location["repository"], location["commit"], location["path"])
            verified = received_source_artifacts([*prior_receipts, *provider.receipts], scoping)
            if calls >= 4 or provider.termination_uncertain:
                break
        if not controls:
            # A declared checked-in build variant locates a neighboring build
            # script to read, not an assertion that the script exists or passes.
            for artifact in verified:
                if "/demo/revisions/" in artifact["path"]:
                    package = artifact["path"].split("/demo/revisions/", 1)[0]
                    await fetch(artifact["repository"], artifact["base_ref"], package + "/demo/build_revision.py")
            return
        # CI reads are grounded in a repository whose actual bytes match the
        # deployed manifest. This immutable commit is not claimed to be current head.
        repositories = list(dict.fromkeys((item["repository"], item["base_ref"]) for item in verified))
        for repository, commit in repositories:
            if calls >= 4 or provider.termination_uncertain:
                break
            if await fetch(repository, commit, ".github/workflows"):
                document = provider.receipts[-1]["result"]
                paths = sorted(
                    {
                        item["path"]
                        for item in _objects(document)
                        if item.get("type") == "file"
                        and isinstance(item.get("path"), str)
                        and item["path"].startswith(".github/workflows/")
                        and item["path"].endswith((".yml", ".yaml"))
                        and ".." not in item["path"].split("/")
                    }
                )
                for path in paths[:2]:
                    await fetch(repository, commit, path)
            await fetch(repository, commit, "package.json")
        if calls >= 4:
            result["warnings"].append("bounded repository read sample; not complete CI coverage")

    provider.add_consumer(consumer)
    try:
        asyncio.run(collect())
    except InvocationStoppedError:
        raise
    except Exception as exc:
        control()
        result["warnings"].append(f"repository collection unavailable: {type(exc).__name__}")
    finally:
        provider.remove_consumer(consumer)
    result["receipts"] = provider.receipts
    if not provider.termination_uncertain:
        result["source_artifacts"] = received_source_artifacts(provider.receipts, scoping)
        result["control_artifacts"] = received_control_artifacts(provider.receipts)
    else:
        result["warnings"].append("repository request lifetime unconfirmed; receipts are diagnostic only")
    return result
