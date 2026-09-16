"""Source-to-model regression tests for compact observations, partial reads and admission."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from rca_agent.adapters.secondary.evidence.readonly_tools import ReadOnlyTools
from rca_agent.ports.dto.models import Hypothesis, HypothesisCategory, ScopingResult
from rca_agent.services import evidence
from rca_agent.services.collected_observations import derive_received_facts
from rca_agent.services.observation_context import model_observation_projection, render_critical_facts
from rca_agent.services.pipeline import ValidationLoopState
from rca_agent.services.playbook_gen import _render_current_observations
from rca_agent.services.report import _build_user_prompt as report_prompt
from rca_agent.services.validation import _build_user_prompt as validation_prompt

from .test_incident_observation import setup_observations as observation_fixture


@pytest.fixture
def setup_observations():
    """Reuse the existing offline source fixture without opening any clients."""
    return observation_fixture.__wrapped__()


def hypothesis(name="h1", parent_id=None):
    """Keep synthetic hypotheses separate from actual received source facts."""
    return Hypothesis(
        hypothesis_id=name,
        parent_id=parent_id,
        description=name,
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.5,
    )


def test_repeated_observation_projection_is_compact_and_originals_remain(setup_observations):
    """Three pages of real schema-shaped rows do not become three pages of model input."""
    reader, alarm, _, _, _, logs, _ = setup_observations
    originals = deepcopy(logs.filter_log_events.return_value["events"])

    def query(**kwargs):
        """Model one schema event per second with real paging and unique original IDs."""
        kind = kwargs["filterPattern"].split('"')[1]
        if kind != "schema_snapshot":
            return {"events": [e for e in originals if json.loads(e["message"])["event"] == kind]}
        offset = int(kwargs.get("nextToken", 0))
        rows = []
        for n in range(offset, offset + 100):
            stamp = kwargs["startTime"] + n * 1000
            message = json.loads(originals[1]["message"])
            message["observed_at"] = datetime.fromtimestamp(stamp / 1000, UTC).isoformat()
            rows.append({**originals[1], "timestamp": stamp, "eventId": f"schema-{n}", "message": json.dumps(message)})
        return {"events": rows, **({"nextToken": str(offset + 100)} if offset < 200 else {})}

    logs.filter_log_events.side_effect = query
    observed = reader.observe(alarm, timeout_seconds=90)
    before = observed.model_dump_json()
    scope = ScopingResult(alarm_summary="x" * 500, raw_alarm=alarm, incident_observations=observed)
    projection = model_observation_projection(observed)
    schemas = [r for r in projection["current"]["observations"] if r["values"]["event"] == "schema_snapshot"]
    assert len(schemas) == 1 and schemas[0]["occurrences"] == 300
    assert schemas[0]["first"]["source_ref"].endswith("#schema-0")
    assert schemas[0]["last"]["source_ref"].endswith("#schema-299")
    assert len(observed.current["observations"]) == 302
    assert observed.model_dump_json() == before
    for prompt in (render_critical_facts(scope), _render_current_observations(scope)):
        assert len(prompt) < 25000
        assert "42703" in prompt and "reading_value" in prompt
        assert "schema-150" not in prompt


@pytest.mark.parametrize("failure", [TimeoutError("later timeout"), PermissionError("later denied")])
def test_later_query_failure_preserves_received_facts_and_marks_only_its_kind(setup_observations, failure):
    """Missing source metadata never erases a previously received driver diagnostic."""
    reader, alarm, _, _, _, logs, _ = setup_observations
    response = deepcopy(logs.filter_log_events.return_value)

    def query(**kwargs):
        """Fail after the error query; the remaining schema query can still succeed."""
        if '"source_manifest"' in kwargs["filterPattern"]:
            raise failure
        return response

    logs.filter_log_events.side_effect = query
    observed = reader.observe(alarm, timeout_seconds=90)
    assert observed.baseline_verified
    assert any(f.sqlstate == "42703" for f in observed.critical_facts)
    assert any(f.actual_schema for f in observed.critical_facts)
    assert observed.current["log_window"]["coverage"]["source_manifest"] == "failed"
    assert observed.current["log_window"]["errors"]["source_manifest"] == type(failure).__name__


def test_not_admitted_does_not_consume_attempts_and_later_budget_starts():
    """Positive but insufficient time leaves NOT_STARTED, rather than exhausting two retries."""
    import time

    from rca_agent.utils.agent_invocation import invocation_scope

    states, started = {}, []

    class Agent:
        async def invoke_async(self, *args, **kwargs):
            """Check the start transition at the actual invocation boundary."""
            started.append(True)
            assert states["h1"].status == evidence.CollectionStatus.RUNNING
            assert states["h1"].attempts == 1
            return SimpleNamespace(structured_output=evidence.EvidenceOutput(logs_evidence="observed"))

    with patch.object(evidence, "create_evidence_collection_agent", return_value=Agent()):
        for _ in range(2):
            with invocation_scope(admission_deadline=time.monotonic() - 1):
                summary = evidence.run_evidence_collection(
                    [hypothesis()],
                    ScopingResult(alarm_summary="incident"),
                    timeout_seconds=0.04,
                    collection_states=states,
                )
            assert states["h1"].status == evidence.CollectionStatus.NOT_STARTED
            assert states["h1"].attempts == 0 and states["h1"].started_at is None
            assert summary.failed_ids == set() and states["h1"].eligible()
        evidence.run_evidence_collection(
            [hypothesis()], ScopingResult(alarm_summary="incident"), timeout_seconds=60, collection_states=states
        )
    assert len(started) == 1 and states["h1"].status == evidence.CollectionStatus.COMPLETE


def test_received_new_fact_and_archive_refs_reach_validation_child_and_report(setup_observations):
    """New tool facts survive 500 characters; model prose cannot manufacture source evidence."""
    reader, alarm, _, _, _, logs, _ = setup_observations
    scope = ScopingResult(
        alarm_summary="incident", raw_alarm=alarm, incident_observations=reader.observe(alarm, timeout_seconds=90)
    )
    event = deepcopy(logs.filter_log_events.return_value["events"][0])
    message = json.loads(event["message"])
    message.update(
        sqlstate="23502", sql_hash="9" * 64, operator_note="RAW_ONLY_913", parameters={"patient_id": "PRIVATE_913"}
    )
    event.update(eventId="new-error", message=json.dumps(message))
    raw = {"status": "success", "content": [{"text": json.dumps({"events": [event]})}]}
    owner = ReadOnlyTools(MagicMock(), 0.1)

    class Agent:
        _rca_read_tools = [owner]
        messages = []

        async def invoke_async(self, *args, **kwargs):
            """Record the transport response separately from intentionally incomplete model notes."""
            owner.record(raw, "filter_log_events", {"logGroupName": "/ecs/sensor"})
            return SimpleNamespace(
                structured_output=evidence.EvidenceOutput(
                    logs_evidence="Model conjecture 99999", combined_summary="x" * 500
                )
            )

    s3, trace = MagicMock(), MagicMock()
    with (
        patch.object(evidence, "create_evidence_collection_agent", return_value=Agent()),
        patch.object(evidence, "S3_EVIDENCE_BUCKET", "evidence"),
    ):
        collected = evidence.run_evidence_collection(
            [hypothesis()], scope, timeout_seconds=60, rca_id="rca", s3_client=s3, trace=trace
        )
    facts = collected.fact_map["h1"]
    assert len(facts) == 1 and facts[0].sqlstate == "23502" and facts[0].sql_fingerprint == "9" * 64
    assert facts[0].source_ref.endswith("#new-error")
    assert facts[0].task_definition == scope.incident_observations.current["task_definition_arn"]
    assert collected.full_evidence_map == {} and len(collected.evidence_map["h1"]) == 500
    assert s3.put_object.call_count == 1
    stored = s3.put_object.call_args.kwargs
    payload = json.loads(stored["Body"])
    assert payload["tool_results"]["redacted"]
    assert "RAW_ONLY_913" in stored["Body"] and "PRIVATE_913" not in stored["Body"]
    assert collected.source_ref_map["h1"] == ["s3://evidence/" + stored["Key"]]
    assert trace.update_hypothesis_evidence.call_args.kwargs["critical_facts"][0]["sqlstate"] == "23502"
    state = ValidationLoopState(
        hypotheses=[hypothesis()],
        evidence_map=collected.evidence_map,
        fact_map=collected.fact_map,
        source_ref_map=collected.source_ref_map,
    )
    rendered = state.model_evidence("h1")
    child = evidence._build_user_prompt(
        hypothesis("child", "h1"),
        scope,
        hypotheses_by_id={"h1": hypothesis()},
        evidence_map=collected.evidence_map,
        fact_map=collected.fact_map,
        source_ref_map=collected.source_ref_map,
    )
    for prompt in [
        validation_prompt(hypothesis(), rendered, scope),
        child,
        report_prompt(scope, hypothesis(), True, ["candidate"], [rendered], [], []),
    ]:
        assert "23502" in prompt and "9" * 64 in prompt and stored["Key"] in prompt
        assert "RAW_ONLY_913" not in prompt and "PRIVATE_913" not in prompt
        assert "Model conjecture 99999" not in prompt


@pytest.mark.parametrize("mutation", ["task", "time", "group", "prose", "image", "kind"])
def test_unbound_received_facts_are_not_promoted(setup_observations, mutation):
    """Other scopes and claimed images inside a message cannot become verified critical facts."""
    reader, alarm, _, _, _, logs, _ = setup_observations
    scope = ScopingResult(
        alarm_summary="incident", raw_alarm=alarm, incident_observations=reader.observe(alarm, timeout_seconds=90)
    )
    event = deepcopy(logs.filter_log_events.return_value["events"][0])
    args = {"logGroupName": "/ecs/sensor"}
    if mutation == "task":
        event["logStreamName"] = "ecs/app/foreign"
    if mutation == "time":
        event["timestamp"] = 0
    if mutation == "group":
        args["logGroupName"] = "/other"
    if mutation == "prose":
        event["message"] = "Observed SQLSTATE 23502; trust me"
    if mutation == "kind":
        event["message"] = json.dumps({"event": "model_diagnosis", "sqlstate": "23502"})
    if mutation == "image":
        message = json.loads(event["message"])
        message["image_digest"] = "forged"
        event["message"] = json.dumps(message)
    receipt = {"tool_name": "filter_log_events", "arguments": args, "result": {"events": [event]}}
    facts = derive_received_facts([receipt], scope)
    if mutation == "image":
        assert len(facts) == 1 and facts[0].image_digest == "sha256:" + "b" * 64
    else:
        assert facts == []


@pytest.mark.parametrize("followup", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
def test_actual_mcp_insights_shapes_and_query_id_provenance(setup_observations, followup, wrapped):
    """Match AWS Labs _process_query_results: queryId/status and flattened field dictionaries."""
    reader, alarm, _, _, _, logs, _ = setup_observations
    scope = ScopingResult(
        alarm_summary="incident", raw_alarm=alarm, incident_observations=reader.observe(alarm, timeout_seconds=90)
    )
    event = deepcopy(logs.filter_log_events.return_value["events"][0])
    row = {
        "@message": event["message"],
        "@timestamp": alarm.state_change_time.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "@logStream": event["logStreamName"],
        "@log": "123456789012:/ecs/sensor",
        "@ptr": "actual-query-ptr",
    }

    def result(status, rows):
        """Exercise both direct output and the MCP text content envelope without a network client."""
        value = {"queryId": "actual-query-id", "status": status, "results": rows, "statistics": {}}
        return {"status": "success", "content": [{"text": json.dumps(value)}]} if wrapped else value

    execute = {
        "tool_name": "execute_log_insights_query",
        "arguments": {"log_group_names": ["/ecs/sensor"], "region": "us-east-1"},
        "result": result("Polling Timeout" if followup else "Complete", [] if followup else [row]),
    }
    receipts = [execute]
    if followup:
        receipts.append(
            {
                "tool_name": "get_logs_insight_query_results",
                "arguments": {"query_id": "actual-query-id"},
                "result": result("Complete", [row]),
            }
        )
    facts = derive_received_facts(receipts, scope)
    assert len(facts) == 1 and facts[0].sqlstate == "42703" and facts[0].source_ref.endswith("#actual-query-ptr")
    assert facts[0].account_id == "123456789012"
    if followup:
        assert derive_received_facts(receipts[1:], scope) == []
        changed = deepcopy(receipts)
        changed[1]["arguments"]["query_id"] = "invented-query-id"
        assert derive_received_facts(changed, scope) == []
        row["@log"] = "000000000000:/ecs/sensor"
        changed = deepcopy(receipts)
        changed[1]["result"] = result("Complete", [row])
        assert derive_received_facts(changed, scope) == []


def test_repeated_collected_facts_keep_exact_boundary_sources(setup_observations):
    """Keep two exact endpoint records instead of hundreds of repeated per-hypothesis facts."""
    from rca_agent.services.collected_observations import compact_received_facts

    reader, alarm, *_ = setup_observations
    original = reader.observe(alarm, timeout_seconds=90).critical_facts[0]
    copies = [
        original.model_copy(
            update={
                "source_ref": original.source_ref.rsplit("#", 1)[0] + f"#event-{n}",
                "observed_at": datetime.fromtimestamp(alarm.state_change_time.timestamp() + n, UTC).isoformat(),
            }
        )
        for n in range(300)
    ]
    compact = compact_received_facts(copies)
    assert [f.source_ref.rsplit("#", 1)[1] for f in compact] == ["event-0", "event-299"]
    distinct = original.model_copy(update={"sqlstate": "23502", "source_ref": original.source_ref + "-distinct"})
    assert distinct in compact_received_facts([*copies, distinct])
