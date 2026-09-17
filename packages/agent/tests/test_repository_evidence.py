"""Actual reader/provider/caller integration using explicitly local transport fixtures."""

import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import Tool

from rca_agent.ports.dto.models import ScopingResult
from rca_agent.services import analysis_workflow
from rca_agent.services.repository_evidence import collect_repository_evidence, source_locations
from rca_agent.utils.agent_invocation import InvocationStoppedError
from tests.test_analysis_workflow import wired_pipeline

pytest_plugins = ["tests.test_incident_observation", "tests.test_analysis_parts"]


class RepositoryClient:
    """Return actual local file bytes in MCP responses; never claim these are remote receipts."""

    def __init__(self, files):
        """Keep deterministic fixture paths and all actual calls for assertions."""
        self.files = files
        self.calls = []
        self.consumers = set()
        self.delay = 0

    def add_consumer(self, consumer_id):
        """Track ownership without starting any network session."""
        self.consumers.add(consumer_id)

    def remove_consumer(self, consumer_id):
        """Release only this test consumer."""
        self.consumers.remove(consumer_id)

    async def load_tools(self):
        """Expose the same metadata consumed by the real readonly wrapper."""
        return [
            SimpleNamespace(
                mcp_tool=Tool(name="get_file_contents", inputSchema={"type": "object"}),
                mcp_client=self,
                tool_name="get_file_contents",
            )
        ]

    async def call_tool_async(self, **kwargs):
        """Preserve immutable request arguments and correlate each local response."""
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay)
        path = kwargs["arguments"]["path"]
        if path == ".github/workflows":
            body = [
                {"type": "file", "path": name}
                for name in self.files
                if name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))
            ]
        elif path in self.files:
            raw = self.files[path].encode()
            body = {
                "path": path,
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
                "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
            }
        else:
            return {
                "toolUseId": kwargs["tool_use_id"],
                "status": "error",
                "isError": True,
                "content": [{"text": "fixture file unavailable"}],
            }
        return {
            "toolUseId": kwargs["tool_use_id"],
            "status": "success",
            "isError": False,
            "content": [{"text": json.dumps(body)}],
        }


@pytest.fixture
def native_repository_scope(setup_observations, tmp_path):
    """Compile real checked-in v1/v2 bytes, then observe their manifests through the native reader."""
    root = Path(__file__).resolve().parents[3]
    path = root / "packages/healthcare-sensor-app/demo/build_revision.py"
    spec = importlib.util.spec_from_file_location("repository_fixture_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    normal = module.compile_revision("v1", tmp_path / "v1", source_repository="team/service", source_commit="a" * 40)
    fault = module.compile_revision("v2", tmp_path / "v2", source_repository="team/service", source_commit="a" * 40)
    reader, alarm, baseline, publish, _, logs, _ = setup_observations
    baseline["observations"][0]["message"].update(normal)
    for event in logs.filter_log_events.return_value["events"]:
        message = json.loads(event["message"])
        if message["event"] == "source_manifest":
            message.update(fault)
            event["message"] = json.dumps(message)
    publish()
    scope = ScopingResult(
        alarm_summary="local compiled fixture",
        raw_alarm=alarm,
        incident_observations=reader.observe(alarm, timeout_seconds=90),
    )
    assert scope.incident_observations.baseline_verified
    files = {
        normal["source_locations"]["revision/write.py"]["path"]: (
            tmp_path / "v1/test_service/revision/write.py"
        ).read_text(),
        fault["source_locations"]["revision/write.py"]["path"]: (
            tmp_path / "v2/test_service/revision/write.py"
        ).read_text(),
        ".github/workflows/ci.yml": (root / ".github/workflows/ci.yml").read_text(),
        "package.json": (root / "package.json").read_text(),
        "packages/healthcare-sensor-app/demo/build_revision.py": path.read_text(),
    }
    return scope, RepositoryClient(files), reader


def test_native_locator_survives_read_and_refresh_and_actual_file_hash_join(native_repository_scope):
    """Declared locations guide real wrapper calls; only returned bytes matching manifests become artifacts."""
    scope, client, reader = native_repository_scope
    before = scope.model_dump(mode="json")
    locations = source_locations(scope)
    assert len(locations) == 2
    refreshed = reader.refresh_current(scope.raw_alarm, scope.incident_observations, timeout_seconds=300)
    assert refreshed.current["observations"] == scope.incident_observations.current["observations"]
    result = collect_repository_evidence(scope, client)
    assert len(client.calls) == 3 and not client.consumers
    assert len(result["source_artifacts"]) == 2
    current = next(item for item in result["source_artifacts"] if item["source_phase"] == "current")
    assert 'TIMESTAMP_COLUMN = "sampled_at"' in current["text"]
    assert "/demo/revisions/v2/revision/write.py" in current["path"]
    assert current["base_ref"] == "a" * 40 and current["is_current_target"] is False
    assert current["identity_kind"] == "git_commit"
    assert all(call["arguments"]["ref"] == "a" * 40 for call in client.calls)
    assert scope.model_dump(mode="json") == before


def test_deliberate_ci_collection_without_root_ci_receipts(native_repository_scope):
    """Operations reads actual CI even when the Root collector fetched application source only."""
    scope, client, _ = native_repository_scope
    root = collect_repository_evidence(scope, client)
    assert not any(item["path"].startswith(".github/workflows/") for item in root["control_artifacts"])
    client.calls.clear()
    result = collect_repository_evidence(scope, client, controls=True, prior_receipts=root["receipts"])
    assert [call["arguments"]["path"] for call in client.calls] == [
        ".github/workflows",
        ".github/workflows/ci.yml",
        "package.json",
    ]
    assert len(result["control_artifacts"]) == 2
    assert all(item["source_kind"] == "read_control_configuration" for item in result["control_artifacts"])
    assert all(item["base_ref"] == "a" * 40 and not item["is_current_target"] for item in result["control_artifacts"])
    assert all(item["source_ref"].startswith("mcp-tool-result://") for item in result["control_artifacts"])


@pytest.mark.parametrize("change", ["missing", "branch", "path", "hash", "bytes", "permission", "eval"])
def test_invalid_or_unavailable_repository_evidence_never_promotes_source(native_repository_scope, change):
    """A locator is neither proof nor permission to guess a repository, path or source body."""
    scope, client, _ = native_repository_scope
    scope.incident_observations.baseline["observations"] = []
    message = next(
        row["message"]
        for row in scope.incident_observations.current["observations"]
        if row["message"]["event"] == "source_manifest"
    )
    location = message["source_locations"]["revision/write.py"]
    if change == "missing":
        message.pop("source_locations")
    elif change == "branch":
        location["commit"] = "main"
    elif change == "path":
        location["path"] = "../revision/write.py"
    elif change == "hash":
        location["sha256"] = "0" * 64
    elif change == "bytes":
        client.files[location["path"]] = "foreign bytes"
    elif change == "permission":
        client.files.clear()
    else:
        scope.raw_alarm.eval_source_metadata = {}
    result = collect_repository_evidence(scope, client, controls=True)
    assert not result["source_artifacts"] and not result["control_artifacts"]
    if change not in {"bytes", "permission"}:
        assert not client.calls


def test_ownership_loss_drains_read_and_does_not_deliver_source(native_repository_scope):
    """A completed response after cancellation is never returned for part publication."""
    scope, client, _ = native_repository_scope
    client.delay = 0.2

    def control():
        """Lose ownership once the request has started."""
        return not client.calls

    with pytest.raises(InvocationStoppedError):
        collect_repository_evidence(scope, client, control=control)
    assert len(client.calls) == 1 and not client.consumers


def test_full_workflow_delivers_real_source_and_ci_without_execution_dependency(
    native_repository_scope, part_store, monkeypatch
):
    """Keep model calls local doubles but exercise reader, provider, source matching, store and role boundaries."""
    scope, client, _ = native_repository_scope
    orchestrator, container, _, run = wired_pipeline(part_store, monkeypatch)
    container.github_mcp_client = client
    orchestrator._run_scoping.return_value = scope

    def preview(report, incident, agent):
        """Require actually decoded deployed bytes at the production preview boundary."""
        current = next(item for item in incident["source_artifacts"] if item["source_phase"] == "current")
        assert 'TIMESTAMP_COLUMN = "sampled_at"' in current["text"]
        return {"status": "UNAVAILABLE", "files": [], "limitations": ["local model double"]}

    def operations(incident, root_payload, agent):
        """Require deliberate CI reads and keep the original immutable Root result untouched."""
        controls = root_payload["result"]["control_artifacts"]
        assert any(item["path"] == ".github/workflows/ci.yml" for item in controls)
        assert not any(
            item["path"].startswith(".github/workflows/")
            for item in part_store.read_part("rca-1", "root_cause")["payload"]["result"]["control_artifacts"]
        )
        return {"title": "ops fixture", "findings": [], "recommendations": []}

    monkeypatch.setattr(analysis_workflow, "generate_code_preview", preview)
    monkeypatch.setattr(analysis_workflow, "generate_operations", operations)
    assert orchestrator._run_pipeline_in_context(scope.raw_alarm, run)
    root = part_store.read_part("rca-1", "root_cause")["payload"]["result"]
    ops = part_store.read_part("rca-1", "operations")["payload"]["result"]
    assert root["source_artifacts"] and root["repository_collection"]["receipts"]
    assert ops["control_artifacts"] and ops["repository_collection"]["receipts"]
    assert part_store.read_incident("rca-1")["payload"]["scoping"][
        "incident_observations"
    ] == scope.incident_observations.model_dump(mode="json")
