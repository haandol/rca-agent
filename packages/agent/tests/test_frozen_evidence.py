"""Prove live readonly collection and exact-source delivery survive early recovery publication."""

import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rca_agent.ports.dto.models import Hypothesis, HypothesisCategory, ScopingResult
from rca_agent.services import evidence
from rca_agent.services.frozen_evidence import bound_request, received_source_artifacts


def scoped_source():
    """Bind a deployed manifest hash to a historical window and its known task stream."""
    text = "old_value = 1\n"
    files = {"revision/write.py": hashlib.sha256(text.encode()).hexdigest()}
    scope = ScopingResult(alarm_summary="incident")
    scope.incident_observations.baseline = {"scope": {"log_group": "/service"}, "observations": []}
    scope.incident_observations.current = {
        "log_window": {"start": "2026-09-17T00:00:00Z", "end": "2026-09-17T00:05:00Z"},
        "observations": [
            {
                "log_stream": "task/old",
                "message": {
                    "event": "source_manifest",
                    "verified": True,
                    "files": files,
                    "fingerprint": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
                },
            }
        ],
    }
    return scope, text


def source_receipt(text):
    """Represent a successful real MCP file response at an immutable commit, not model prose."""
    return {
        "tool_name": "get_file_contents",
        "request_terminated": True,
        "source_ref": "mcp-tool-result://file/1",
        "arguments": {"owner": "team", "repo": "service", "path": "app/revision/write.py", "ref": "a" * 40},
        "result": {
            "status": "success",
            "content": [
                {
                    "text": json.dumps(
                        {
                            "path": "app/revision/write.py",
                            "content": base64.b64encode(text.encode()).decode(),
                            "encoding": "base64",
                        }
                    )
                }
            ],
        },
    }


def test_bounded_queries_keep_actual_collection_but_refuse_current_time_or_source():
    """Historical Insights calls execute in the original stream scope; current/post-rollback reads do not."""
    scope, _ = scoped_source()
    args = {
        "start_time": "2026-09-17T00:00:00Z",
        "end_time": "2026-09-17T00:05:00Z",
        "log_group_names": ["/service"],
        "query_string": "fields @message | limit 10",
    }
    bound = bound_request(scope, "execute_log_insights_query", args, set())
    assert bound["query_string"].startswith('filter @logStream in ["task/old"] | ')
    assert args["query_string"] == "fields @message | limit 10"
    with pytest.raises(ValueError, match="beyond"):
        bound_request(scope, "execute_log_insights_query", {**args, "end_time": "2026-09-17T00:06:00Z"}, set())
    assert bound_request(scope, "describe_services", {"cluster": "current"}, set()) == {"cluster": "current"}
    with pytest.raises(ValueError):
        bound_request(scope, "get_file_contents", {"ref": "main"}, set())
    assert bound_request(scope, "get_file_contents", {"ref": "a" * 40}, set())["ref"] == "a" * 40


def test_actual_file_response_is_bound_to_observed_manifest_and_rejects_substitutions():
    """Commit-pinned downloaded bytes must also match the deployed source, not merely a plausible path."""
    scope, text = scoped_source()
    receipt = source_receipt(text)
    result = received_source_artifacts([receipt], scope)
    assert result[0]["text"] == text and result[0]["repository"] == "team/service"
    assert result[0]["base_revision"] == "a" * 40
    assert received_source_artifacts([source_receipt("foreign bytes")], scope) == []
    receipt["result"]["status"] = "error"
    assert received_source_artifacts([receipt], scope) == []


def test_live_collection_delivers_received_source_to_code_preview_input(monkeypatch):
    """The production collection function extracts real tool receipts instead of a mock-only container hook."""
    scope, text = scoped_source()
    receipt = source_receipt(text)
    agent = SimpleNamespace(
        _rca_read_tools=[SimpleNamespace(receipts=[receipt], results=[], active=0, termination_uncertain=False)]
    )
    create = Mock(return_value=agent)
    monkeypatch.setattr(evidence, "create_evidence_collection_agent", create)
    monkeypatch.setattr(
        evidence,
        "invoke_agent",
        Mock(
            return_value=evidence.EvidenceOutput(
                code_change_evidence="Observed deployed source", combined_summary="source read"
            )
        ),
    )
    result = evidence.collect_evidence(
        Hypothesis(hypothesis_id="h", description="code", confidence_score=0.5, category=HypothesisCategory.DEPLOYMENT),
        scope,
        mcp_clients=[object()],
    )
    assert create.call_count == 1 and not result.failed
    assert result.source_artifacts[0]["text"] == text
    assert result.source_artifacts[0]["source_ref"] == receipt["source_ref"]
    assert result.raw_tool_outputs


def test_no_baseline_still_permits_real_historical_metrics_logs_and_discovery():
    """Missing rollback evidence is not a permission gate on root investigation."""
    from datetime import UTC, datetime

    from rca_agent.ports.dto.models import AlarmPayload

    scope = ScopingResult(
        alarm_summary="generic",
        raw_alarm=AlarmPayload(alarm_name="alarm", state_change_time=datetime(2026, 9, 17, 0, 5, tzinfo=UTC)),
    )
    metric = {
        "start_time": "2026-09-17T00:00:00Z",
        "end_time": "2026-09-17T00:05:00Z",
        "namespace": "AWS/RDS",
        "metric_name": "DatabaseConnections",
        "dimensions": {"DBInstanceIdentifier": "observed-db"},
    }
    assert bound_request(scope, "get_metric_data", metric, set()) == metric
    logs = {
        "start_time": metric["start_time"],
        "end_time": metric["end_time"],
        "log_group_names": ["/observed/service"],
        "query_string": "fields @message | limit 10",
    }
    assert bound_request(scope, "execute_log_insights_query", logs, set()) == logs
    assert bound_request(scope, "list_metrics", {"namespace": "AWS/ECS"}, set()) == {"namespace": "AWS/ECS"}
    assert bound_request(
        scope,
        "get_metric_data",
        {"start_time": metric["start_time"]},
        set(),
        input_schema={"properties": {"end_time": {"type": "string"}}},
    )["end_time"].startswith("2026-09-17T00:05")


def test_normal_stream_related_metric_and_older_deploy_history_remain_available():
    """Cutoff excludes post-recovery data rather than restricting analysis to one alarm or current task."""
    scope, _ = scoped_source()
    scope.incident_observations.baseline["observations"] = [{"log_stream": "task/normal"}]
    query = {
        "start_time": "2026-08-01T00:00:00Z",
        "end_time": "2026-09-17T00:05:00Z",
        "log_group_names": ["/service"],
        "query_string": "fields @message",
    }
    assert '"task/normal"' in bound_request(scope, "execute_log_insights_query", query, set())["query_string"]
    historical = {"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-09-17T00:00:00Z"}
    assert bound_request(scope, "lookup_events", historical, set()) == historical
    metric = {**historical, "namespace": "AWS/ECS", "metric_name": "CPUUtilization"}
    assert bound_request(scope, "get_metric_data", metric, set()) == metric


def test_ci_source_is_retained_without_claiming_it_was_in_deployed_app_manifest():
    """Actual CI bytes support operations review but cannot be mistaken for deployed application source."""
    from rca_agent.services.frozen_evidence import received_control_artifacts

    scope, _ = scoped_source()
    receipt = source_receipt("name: checks\non: push\n")
    receipt["arguments"]["path"] = ".github/workflows/checks.yml"
    body = json.loads(receipt["result"]["content"][0]["text"])
    body["path"] = receipt["arguments"]["path"]
    receipt["result"]["content"][0]["text"] = json.dumps(body)
    assert received_source_artifacts([receipt], scope) == []
    controls = received_control_artifacts([receipt])
    assert controls[0]["text"].startswith("name: checks")
    assert controls[0]["source_kind"] == "read_control_configuration"


def test_github_embedded_text_resource_is_also_bound_to_commit_path_and_manifest():
    """Support the MCP embedded-resource response without treating arbitrary text as a source file."""
    scope, text = scoped_source()
    receipt = source_receipt(text)
    receipt["result"]["content"] = [
        {
            "type": "resource",
            "resource": {
                "uri": "repo://team/service/" + "a" * 40 + "/contents/app/revision/write.py",
                "mimeType": "text/plain",
                "text": text,
            },
        }
    ]
    assert received_source_artifacts([receipt], scope)[0]["text"] == text
    receipt["result"]["content"][0]["resource"]["uri"] = "repo://foreign/service/contents/app/revision/write.py"
    assert received_source_artifacts([receipt], scope) == []


@pytest.mark.parametrize(
    "suffix,accepted",
    [
        ("sha/{ref}/contents/.github/workflows/ci.yml", True),
        ("{ref}/contents/.github/workflows/ci.yml", True),
        ("contents/.github/workflows/ci.yml?ref={ref}", True),
        ("contents/.github/workflows/ci.yml", True),
        ("sha/{foreign}/contents/.github/workflows/ci.yml", False),
        ("{foreign}/contents/.github/workflows/ci.yml", False),
        ("sha/{ref}/contents/other/.github/workflows/ci.yml", False),
        ("unrecognized/contents/.github/workflows/ci.yml", False),
        ("sha/{ref}/contents/.github/workflows/ci.yml?ref={foreign}", False),
        ("contents/.github/workflows/ci.yml?ref={ref}&ref={foreign}", False),
        ("contents/.github/workflows/ci.yml?ref=", False),
    ],
)
def test_ci_embedded_uri_requires_exact_commit_and_path_without_manifest_fallback(suffix, accepted):
    """CI has no app-manifest hash guard, so URI revision/path must bind to the actual request."""
    from rca_agent.services.frozen_evidence import received_control_artifacts

    receipt = source_receipt("name: local CI fixture\n")
    receipt["arguments"]["path"] = ".github/workflows/ci.yml"
    receipt["result"]["content"] = [
        {
            "type": "resource",
            "resource": {
                "uri": "repo://team/service/" + suffix.format(ref="a" * 40, foreign="b" * 40),
                "mimeType": "text/plain",
                "text": "name: local CI fixture\n",
            },
        }
    ]
    result = received_control_artifacts([receipt])
    assert bool(result) is accepted
    if result:
        assert result[0]["base_ref"] == "a" * 40


@pytest.mark.asyncio
async def test_no_baseline_provider_performs_actual_bounded_metric_call():
    """The actual MCP wrapper admits generic metrics and sends the schema-supported frozen end time."""
    from mcp.types import Tool

    from rca_agent.adapters.secondary.evidence.readonly_tools import ReadOnlyTool, ReadOnlyTools
    from rca_agent.services.frozen_evidence import frozen_evidence_scope

    scope = ScopingResult(alarm_summary="generic CPU alarm")
    object.__setattr__(scope, "_frozen_cutoff", "2026-09-17T00:05:00Z")
    calls = []

    class Client:
        """Replace only transport so the real tool's admission, result and ownership path runs."""

        async def call_tool_async(self, **kwargs):
            """Return a correlated received metric response without a network request."""
            calls.append(kwargs)
            return {"toolUseId": "metric", "status": "success", "content": [{"text": "measured CPU data"}]}

    with frozen_evidence_scope(scope):
        owner = ReadOnlyTools(Client(), 60)
    original = SimpleNamespace(
        mcp_tool=Tool(
            name="get_metric_data",
            inputSchema={
                "type": "object",
                "properties": {"start_time": {"type": "string"}, "end_time": {"type": "string"}},
            },
        ),
        mcp_client=owner.client,
        tool_name="get_metric_data",
    )
    tool = ReadOnlyTool(original, owner)
    outputs = [
        event
        async for event in tool.stream(
            {
                "toolUseId": "metric",
                "input": {
                    "start_time": "2026-09-17T00:00:00Z",
                    "namespace": "AWS/ECS",
                    "metric_name": "CPUUtilization",
                },
            },
            {},
        )
    ]
    assert len(calls) == 1 and outputs
    assert calls[0]["arguments"]["end_time"] == "2026-09-17T00:05:00Z"
    assert owner.receipts[0]["request_terminated"] is True
