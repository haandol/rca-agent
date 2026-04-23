from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from rca_agent.models import Hypothesis, HypothesisCategory, HypothesisStatus
from rca_agent.trace_store import (
    SpanStatus,
    SpanType,
    TraceStore,
    _deserialize_hypothesis,
    _deserialize_span,
)

TABLE_NAME = "rca-sessions"
PATCH_TABLE = "rca_agent.trace_store.DYNAMODB_TABLE_NAME"


@pytest.fixture()
def dynamodb_client() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def trace(dynamodb_client: MagicMock) -> TraceStore:
    with patch(PATCH_TABLE, TABLE_NAME):
        return TraceStore("rca-123", dynamodb_client=dynamodb_client)


def _make_hypothesis(
    hypothesis_id: str = "h-1",
    tree_id: str = "tree-1",
    parent_id: str | None = None,
    depth: int = 0,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        tree_id=tree_id,
        parent_id=parent_id,
        depth=depth,
        description="Test hypothesis",
        category=HypothesisCategory.DEPLOYMENT,
        confidence_score=0.7,
        required_evidence=["logs", "metrics"],
        status=HypothesisStatus.PENDING,
    )


class TestTraceStoreDisabled:
    def test_disabled_when_no_table_name(self, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, ""):
            store = TraceStore("rca-123", dynamodb_client=dynamodb_client)
        span = store.start_span(SpanType.SCOPING)
        store.end_span(span)
        dynamodb_client.put_item.assert_not_called()

    def test_disabled_when_no_client(self):
        with patch(PATCH_TABLE, TABLE_NAME):
            store = TraceStore("rca-123", dynamodb_client=None)
        span = store.start_span(SpanType.SCOPING)
        store.end_span(span)

    def test_put_hypotheses_noop_when_disabled(self, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, ""):
            store = TraceStore("rca-123", dynamodb_client=dynamodb_client)
        store.put_hypotheses([_make_hypothesis()])
        dynamodb_client.batch_write_item.assert_not_called()


class TestStartSpan:
    def test_creates_span_and_writes_to_dynamodb(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            span = trace.start_span(SpanType.SCOPING, input_summary="alarm=HighCPU")

        assert span.span_type == SpanType.SCOPING
        assert span.status == SpanStatus.RUNNING
        assert span.input_summary == "alarm=HighCPU"
        assert span.rca_id == "rca-123"
        dynamodb_client.put_item.assert_called_once()
        item = dynamodb_client.put_item.call_args[1]["Item"]
        assert item["PK"]["S"] == "RCA#rca-123"
        assert item["SK"]["S"].startswith("SPAN#")
        assert item["span_type"]["S"] == "SCOPING"
        assert item["span_status"]["S"] == "RUNNING"

    def test_span_with_parent_and_loop_index(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            span = trace.start_span(
                SpanType.PRIORITIZATION,
                parent_span_id="parent-id",
                loop_index=2,
            )

        item = dynamodb_client.put_item.call_args[1]["Item"]
        assert item["parent_span_id"]["S"] == "parent-id"
        assert item["loop_index"]["N"] == "2"
        assert span.parent_span_id == "parent-id"
        assert span.loop_index == 2

    def test_truncates_long_input_summary(self, trace: TraceStore, dynamodb_client: MagicMock):
        long_summary = "x" * 1000
        with patch(PATCH_TABLE, TABLE_NAME):
            span = trace.start_span(SpanType.SCOPING, input_summary=long_summary)
        assert len(span.input_summary) == 500

    def test_swallows_dynamodb_error(self, trace: TraceStore, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.put_item.side_effect = ClientError(error_response, "PutItem")

        with patch(PATCH_TABLE, TABLE_NAME):
            span = trace.start_span(SpanType.SCOPING)
        assert span.span_type == SpanType.SCOPING


class TestEndSpan:
    def test_updates_span_with_end_data(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            span = trace.start_span(SpanType.SCOPING)
            trace.end_span(span, output_summary="severity=high", metadata={"key": "val"})

        assert span.status == SpanStatus.COMPLETED
        assert span.end_time is not None
        assert span.duration_ms is not None
        assert span.output_summary == "severity=high"
        assert dynamodb_client.update_item.call_count == 1
        call_kwargs = dynamodb_client.update_item.call_args[1]
        assert ":meta" in call_kwargs["ExpressionAttributeValues"]

    def test_end_span_with_error(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            span = trace.start_span(SpanType.SCOPING)
            trace.end_span(span, status=SpanStatus.FAILED, error="timeout")

        assert span.status == SpanStatus.FAILED
        assert span.error == "timeout"
        call_kwargs = dynamodb_client.update_item.call_args[1]
        assert ":err" in call_kwargs["ExpressionAttributeValues"]

    def test_swallows_dynamodb_error(self, trace: TraceStore, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with patch(PATCH_TABLE, TABLE_NAME):
            span = trace.start_span(SpanType.SCOPING)
            dynamodb_client.update_item.side_effect = ClientError(error_response, "UpdateItem")
            trace.end_span(span)


class TestSpanContextManager:
    def test_successful_span(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME), trace.span(SpanType.SCOPING, input_summary="test") as s:
            s.output_summary = "done"

        assert s.status == SpanStatus.COMPLETED
        assert s.output_summary == "done"
        assert dynamodb_client.put_item.call_count == 1
        assert dynamodb_client.update_item.call_count == 1

    def test_failed_span_on_exception(self, trace: TraceStore, dynamodb_client: MagicMock):
        with pytest.raises(ValueError, match="boom"), patch(PATCH_TABLE, TABLE_NAME), trace.span(SpanType.SCOPING) as s:
            raise ValueError("boom")

        assert s.status == SpanStatus.FAILED
        assert s.error == "boom"

    def test_preserves_metadata_on_failure(self, trace: TraceStore, dynamodb_client: MagicMock):
        with pytest.raises(RuntimeError), patch(PATCH_TABLE, TABLE_NAME), trace.span(SpanType.SCOPING) as s:
            s.metadata = {"key": "val"}
            raise RuntimeError("fail")

        call_kwargs = dynamodb_client.update_item.call_args[1]
        assert ":meta" in call_kwargs["ExpressionAttributeValues"]


class TestPutHypotheses:
    def test_writes_hypotheses_in_batch(self, trace: TraceStore, dynamodb_client: MagicMock):
        hypotheses = [_make_hypothesis("h-1"), _make_hypothesis("h-2")]
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.put_hypotheses(hypotheses)

        dynamodb_client.batch_write_item.assert_called_once()
        items = dynamodb_client.batch_write_item.call_args[1]["RequestItems"][TABLE_NAME]
        assert len(items) == 2
        assert items[0]["PutRequest"]["Item"]["SK"]["S"] == "HYPO#h-1"
        assert items[1]["PutRequest"]["Item"]["SK"]["S"] == "HYPO#h-2"

    def test_sets_parent_id_null_for_root(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.put_hypotheses([_make_hypothesis(parent_id=None)])

        item = dynamodb_client.batch_write_item.call_args[1]["RequestItems"][TABLE_NAME][0]
        assert item["PutRequest"]["Item"]["parent_id"] == {"NULL": True}

    def test_sets_parent_id_for_child(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.put_hypotheses([_make_hypothesis(parent_id="p-1", depth=1)])

        item = dynamodb_client.batch_write_item.call_args[1]["RequestItems"][TABLE_NAME][0]
        assert item["PutRequest"]["Item"]["parent_id"]["S"] == "p-1"
        assert item["PutRequest"]["Item"]["depth"]["N"] == "1"

    def test_chunks_large_batches(self, trace: TraceStore, dynamodb_client: MagicMock):
        hypotheses = [_make_hypothesis(f"h-{i}") for i in range(30)]
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.put_hypotheses(hypotheses)

        assert dynamodb_client.batch_write_item.call_count == 2
        first_chunk = dynamodb_client.batch_write_item.call_args_list[0][1]["RequestItems"][TABLE_NAME]
        second_chunk = dynamodb_client.batch_write_item.call_args_list[1][1]["RequestItems"][TABLE_NAME]
        assert len(first_chunk) == 25
        assert len(second_chunk) == 5

    def test_empty_list_noop(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.put_hypotheses([])
        dynamodb_client.batch_write_item.assert_not_called()

    def test_swallows_dynamodb_error(self, trace: TraceStore, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.batch_write_item.side_effect = ClientError(error_response, "BatchWriteItem")

        with patch(PATCH_TABLE, TABLE_NAME):
            trace.put_hypotheses([_make_hypothesis()])


class TestUpdateHypothesisStatus:
    def test_updates_status_and_confidence(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.update_hypothesis_status(
                "h-1",
                status="CONFIRMED",
                confidence=0.95,
                judgment_reasoning="Strong log evidence",
            )

        call_kwargs = dynamodb_client.update_item.call_args[1]
        assert call_kwargs["Key"]["SK"]["S"] == "HYPO#h-1"
        assert ":status" in call_kwargs["ExpressionAttributeValues"]
        assert call_kwargs["ExpressionAttributeValues"][":status"]["S"] == "CONFIRMED"
        assert call_kwargs["ExpressionAttributeValues"][":jc"]["N"] == "0.95"
        assert "judgment_confidence" in call_kwargs["UpdateExpression"]

    def test_updates_without_confidence(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.update_hypothesis_status("h-1", status="REJECTED")

        call_kwargs = dynamodb_client.update_item.call_args[1]
        assert ":jc" not in call_kwargs["ExpressionAttributeValues"]

    def test_swallows_error(self, trace: TraceStore, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with patch(PATCH_TABLE, TABLE_NAME):
            trace.update_hypothesis_status("h-1", status="CONFIRMED")


class TestUpdateHypothesisEvidence:
    def test_updates_evidence_summary(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.update_hypothesis_evidence("h-1", evidence_summary="CPU spike at 12:00")

        call_kwargs = dynamodb_client.update_item.call_args[1]
        assert call_kwargs["Key"]["SK"]["S"] == "HYPO#h-1"
        assert call_kwargs["ExpressionAttributeValues"][":es"]["S"] == "CPU spike at 12:00"

    def test_truncates_long_evidence(self, trace: TraceStore, dynamodb_client: MagicMock):
        with patch(PATCH_TABLE, TABLE_NAME):
            trace.update_hypothesis_evidence("h-1", evidence_summary="x" * 1000)

        call_kwargs = dynamodb_client.update_item.call_args[1]
        assert len(call_kwargs["ExpressionAttributeValues"][":es"]["S"]) == 500

    def test_swallows_error(self, trace: TraceStore, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with patch(PATCH_TABLE, TABLE_NAME):
            trace.update_hypothesis_evidence("h-1", evidence_summary="test")


class TestGetTrace:
    def test_returns_empty_when_disabled(self):
        with patch(PATCH_TABLE, ""):
            result = TraceStore.get_trace("rca-123", dynamodb_client=None)
        assert result == {"session": None, "spans": [], "hypotheses": []}

    def test_returns_structured_trace(self, dynamodb_client: MagicMock):
        dynamodb_client.query.return_value = {
            "Items": [
                {
                    "PK": {"S": "RCA#rca-123"},
                    "SK": {"S": "SESSION"},
                    "state": {"S": "COMPLETED"},
                    "alarm_name": {"S": "HighCPU"},
                    "alarm_arn": {"S": ""},
                    "root_cause": {"S": "Bad deploy"},
                    "confirmed": {"BOOL": True},
                    "created_at": {"S": "2025-06-01T12:00:00"},
                    "updated_at": {"S": "2025-06-01T12:05:00"},
                },
                {
                    "PK": {"S": "RCA#rca-123"},
                    "SK": {"S": "SPAN#span-1"},
                    "span_type": {"S": "SCOPING"},
                    "span_status": {"S": "COMPLETED"},
                    "start_time": {"S": "2025-06-01T12:00:00"},
                    "end_time": {"S": "2025-06-01T12:01:00"},
                    "duration_ms": {"N": "60000"},
                    "input_summary": {"S": "alarm=HighCPU"},
                    "output_summary": {"S": "severity=high"},
                },
                {
                    "PK": {"S": "RCA#rca-123"},
                    "SK": {"S": "HYPO#h-1"},
                    "tree_id": {"S": "tree-1"},
                    "parent_id": {"NULL": True},
                    "depth": {"N": "0"},
                    "description": {"S": "Bad deploy"},
                    "category": {"S": "DEPLOYMENT"},
                    "confidence_score": {"N": "0.9"},
                    "status": {"S": "CONFIRMED"},
                    "required_evidence": {"L": [{"S": "logs"}]},
                    "evidence_summary": {"S": "Found deploy error"},
                    "judgment_reasoning": {"S": "Logs confirm deploy failure"},
                    "judgment_confidence": {"N": "0.95"},
                    "created_at": {"S": "2025-06-01T12:00:00"},
                    "updated_at": {"S": "2025-06-01T12:03:00"},
                },
            ],
        }

        with patch(PATCH_TABLE, TABLE_NAME):
            result = TraceStore.get_trace("rca-123", dynamodb_client=dynamodb_client)

        assert result["session"]["state"] == "COMPLETED"
        assert result["session"]["alarm_name"] == "HighCPU"
        assert len(result["spans"]) == 1
        assert result["spans"][0]["span_type"] == "SCOPING"
        assert result["spans"][0]["duration_ms"] == 60000
        assert len(result["hypotheses"]) == 1
        assert result["hypotheses"][0]["description"] == "Bad deploy"
        assert result["hypotheses"][0]["judgment_confidence"] == 0.95
        assert result["hypotheses"][0]["parent_id"] is None

    def test_returns_empty_on_error(self, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.query.side_effect = ClientError(error_response, "Query")

        with patch(PATCH_TABLE, TABLE_NAME):
            result = TraceStore.get_trace("rca-123", dynamodb_client=dynamodb_client)

        assert result == {"session": None, "spans": [], "hypotheses": []}


class TestDeserializeSpan:
    def test_deserializes_full_span(self):
        item = {
            "PK": {"S": "RCA#rca-123"},
            "SK": {"S": "SPAN#span-1"},
            "span_type": {"S": "VALIDATION"},
            "span_status": {"S": "COMPLETED"},
            "parent_span_id": {"S": "loop-1"},
            "loop_index": {"N": "2"},
            "start_time": {"S": "2025-06-01T12:00:00"},
            "end_time": {"S": "2025-06-01T12:01:00"},
            "duration_ms": {"N": "60000"},
            "input_summary": {"S": "test input"},
            "output_summary": {"S": "test output"},
            "error": {"S": "some error"},
            "metadata": {"M": {"count": {"N": "5"}, "flag": {"BOOL": True}}},
        }
        result = _deserialize_span(item)
        assert result["span_id"] == "span-1"
        assert result["parent_span_id"] == "loop-1"
        assert result["loop_index"] == 2
        assert result["error"] == "some error"
        assert result["metadata"]["count"] == 5
        assert result["metadata"]["flag"] is True


class TestDeserializeHypothesis:
    def test_deserializes_with_parent(self):
        item = {
            "PK": {"S": "RCA#rca-123"},
            "SK": {"S": "HYPO#h-2"},
            "tree_id": {"S": "tree-1"},
            "parent_id": {"S": "h-1"},
            "depth": {"N": "1"},
            "description": {"S": "Child hypothesis"},
            "category": {"S": "INFRASTRUCTURE"},
            "confidence_score": {"N": "0.6"},
            "status": {"S": "NEEDS_INVESTIGATION"},
            "required_evidence": {"L": [{"S": "metrics"}, {"S": "traces"}]},
            "evidence_summary": {"S": ""},
            "judgment_reasoning": {"S": ""},
            "created_at": {"S": "2025-06-01T12:02:00"},
            "updated_at": {"S": "2025-06-01T12:02:00"},
        }
        result = _deserialize_hypothesis(item)
        assert result["hypothesis_id"] == "h-2"
        assert result["parent_id"] == "h-1"
        assert result["depth"] == 1
        assert result["required_evidence"] == ["metrics", "traces"]
        assert result["judgment_confidence"] is None
