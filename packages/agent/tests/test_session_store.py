from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    SessionCancelledError,
    build_idempotency_key,
    build_rca_id,
    check_duplicate,
    create_session,
    mark_completed,
    mark_failed,
    update_state,
)
from rca_agent.ports.dto.models import AlarmPayload, AlarmTrigger, RcaSessionState


@pytest.fixture()
def alarm() -> AlarmPayload:
    return AlarmPayload(
        alarm_name="HighCPU",
        alarm_arn="arn:aws:cloudwatch:us-east-1:123456789012:alarm:HighCPU",
        state_change_time=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
        trigger=AlarmTrigger(
            metric_name="CPUUtilization",
            namespace="AWS/ECS",
        ),
    )


@pytest.fixture()
def dynamodb_client() -> MagicMock:
    return MagicMock()


class TestBuildIdempotencyKey:
    def test_builds_key_from_alarm(self, alarm: AlarmPayload):
        key = build_idempotency_key(alarm)
        assert key == "HighCPU#2025-06-01T12:00:00+00:00"

    def test_builds_key_without_timestamp(self):
        alarm = AlarmPayload(alarm_name="NoTimestamp")
        key = build_idempotency_key(alarm)
        assert key == "NoTimestamp#unknown"


class TestBuildRcaId:
    def test_deterministic(self):
        key = "HighCPU#2025-06-01T12:00:00+00:00"
        id1 = build_rca_id(key)
        id2 = build_rca_id(key)
        assert id1 == id2

    def test_returns_valid_uuid(self):
        rca_id = build_rca_id("some-key")
        parsed = uuid.UUID(rca_id)
        assert parsed.version == 5

    def test_different_keys_produce_different_ids(self):
        id1 = build_rca_id("key-a")
        id2 = build_rca_id("key-b")
        assert id1 != id2

    def test_uses_namespace_url(self):
        key = "test-key"
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        assert build_rca_id(key) == expected


class TestCreateSession:
    def test_returns_none_when_no_table_name(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = create_session(alarm, dynamodb_client=dynamodb_client)
        assert result is None
        dynamodb_client.put_item.assert_not_called()

    def test_returns_none_when_no_client(self, alarm: AlarmPayload):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = create_session(alarm, dynamodb_client=None)
        assert result is None

    def test_creates_session_successfully(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            session = create_session(alarm, dynamodb_client=dynamodb_client)

        assert session is not None
        assert session.rca_id
        assert session.alarm_name == "HighCPU"
        assert session.state == RcaSessionState.ALARM_RECEIVED
        assert session.idempotency_key == "HighCPU#2025-06-01T12:00:00+00:00"
        assert session.engine == "strands"
        dynamodb_client.put_item.assert_called_once()

    def test_rca_id_is_deterministic(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            session = create_session(alarm, dynamodb_client=dynamodb_client)

        expected_rca_id = build_rca_id(build_idempotency_key(alarm))
        assert session.rca_id == expected_rca_id

    def test_put_item_uses_correct_table(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "my-table"):
            create_session(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.put_item.call_args[1]
        assert call_kwargs["TableName"] == "my-table"

    def test_put_item_uses_engine_prefixed_sk(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            create_session(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.put_item.call_args[1]
        assert call_kwargs["Item"]["SK"]["S"] == "strands#SESSION"

    def test_put_item_includes_engine_attribute(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            create_session(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.put_item.call_args[1]
        assert call_kwargs["Item"]["engine"]["S"] == "strands"

    def test_put_item_includes_condition_expression(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            create_session(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.put_item.call_args[1]
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(SK)"

    def test_returns_none_on_conditional_check_failed(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "dup"}}
        dynamodb_client.put_item.side_effect = ClientError(error_response, "PutItem")

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = create_session(alarm, dynamodb_client=dynamodb_client)

        assert result is None

    def test_raises_on_other_client_error(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.put_item.side_effect = ClientError(error_response, "PutItem")

        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"),
            pytest.raises(ClientError),
        ):
            create_session(alarm, dynamodb_client=dynamodb_client)


class TestCheckDuplicate:
    def test_returns_false_when_no_table_name(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = check_duplicate(alarm, dynamodb_client=dynamodb_client)
        assert result is False

    def test_returns_false_when_no_client(self, alarm: AlarmPayload):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = check_duplicate(alarm, dynamodb_client=None)
        assert result is False

    def test_returns_true_when_item_found(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        dynamodb_client.get_item.return_value = {"Item": {"PK": {"S": "RCA#abc"}, "SK": {"S": "strands#SESSION"}}}

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = check_duplicate(alarm, dynamodb_client=dynamodb_client)

        assert result is True

    def test_returns_false_when_no_item(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        dynamodb_client.get_item.return_value = {}

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = check_duplicate(alarm, dynamodb_client=dynamodb_client)

        assert result is False

    def test_returns_false_on_error(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        dynamodb_client.get_item.side_effect = ClientError(error_response, "GetItem")

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = check_duplicate(alarm, dynamodb_client=dynamodb_client)

        assert result is False

    def test_uses_get_item_with_correct_key(self, alarm: AlarmPayload, dynamodb_client: MagicMock):
        dynamodb_client.get_item.return_value = {}

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            check_duplicate(alarm, dynamodb_client=dynamodb_client)

        call_kwargs = dynamodb_client.get_item.call_args[1]
        expected_rca_id = build_rca_id(build_idempotency_key(alarm))
        assert call_kwargs["Key"]["PK"]["S"] == f"RCA#{expected_rca_id}"
        assert call_kwargs["Key"]["SK"]["S"] == "strands#SESSION"


def _ddb_with_state(state: str) -> MagicMock:
    """Create a MagicMock DDB client that returns the given state for get_item."""
    ddb = MagicMock()
    ddb.get_item.return_value = {"Item": {"state": {"S": state}}}
    return ddb


class TestUpdateState:
    def test_returns_false_when_no_table(self, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = update_state("rca-123", RcaSessionState.SCOPING, dynamodb_client=dynamodb_client)
        assert result is False

    def test_returns_false_when_no_client(self):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = update_state("rca-123", RcaSessionState.SCOPING, dynamodb_client=None)
        assert result is False

    def test_updates_state_successfully(self):
        ddb = _ddb_with_state("ALARM_RECEIVED")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = update_state("rca-123", RcaSessionState.SCOPING, dynamodb_client=ddb)

        assert result is True
        ddb.update_item.assert_called_once()
        call_kwargs = ddb.update_item.call_args[1]
        assert call_kwargs["Key"]["PK"]["S"] == "RCA#rca-123"
        assert call_kwargs["Key"]["SK"]["S"] == "strands#SESSION"
        assert call_kwargs["ExpressionAttributeValues"][":state"]["S"] == "SCOPING"

    def test_includes_cancelled_condition(self):
        ddb = _ddb_with_state("ALARM_RECEIVED")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            update_state("rca-123", RcaSessionState.SCOPING, dynamodb_client=ddb)

        call_kwargs = ddb.update_item.call_args[1]
        assert "ConditionExpression" in call_kwargs
        assert call_kwargs["ExpressionAttributeValues"][":cancelled"]["S"] == "CANCELLED"

    def test_raises_session_cancelled_error(self):
        ddb = _ddb_with_state("ALARM_RECEIVED")
        error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "cancelled"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with (
            patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"),
            pytest.raises(SessionCancelledError),
        ):
            update_state("rca-123", RcaSessionState.SCOPING, dynamodb_client=ddb)

    def test_returns_false_on_error(self):
        ddb = _ddb_with_state("ALARM_RECEIVED")
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = update_state("rca-123", RcaSessionState.SCOPING, dynamodb_client=ddb)

        assert result is False


class TestMarkCompleted:
    def test_returns_false_when_no_table(self, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = mark_completed("rca-123", dynamodb_client=dynamodb_client)
        assert result is False

    def test_marks_completed_with_root_cause(self):
        ddb = _ddb_with_state("REPORT_GENERATION")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_completed(
                "rca-123",
                root_cause="Bad deploy",
                confirmed=True,
                dynamodb_client=ddb,
            )

        assert result is True
        call_kwargs = ddb.update_item.call_args[1]
        assert call_kwargs["Key"]["SK"]["S"] == "strands#SESSION"
        assert call_kwargs["ExpressionAttributeValues"][":state"]["S"] == "COMPLETED"
        assert call_kwargs["ExpressionAttributeValues"][":rc"]["S"] == "Bad deploy"
        assert call_kwargs["ExpressionAttributeValues"][":cf"]["BOOL"] is True

    def test_returns_false_on_error(self):
        ddb = _ddb_with_state("REPORT_GENERATION")
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_completed("rca-123", dynamodb_client=ddb)

        assert result is False


class TestMarkFailed:
    def test_returns_false_when_no_table(self, dynamodb_client: MagicMock):
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", ""):
            result = mark_failed("rca-123", dynamodb_client=dynamodb_client)
        assert result is False

    def test_marks_failed_with_reason(self):
        ddb = _ddb_with_state("SCOPING")
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_failed(
                "rca-123",
                error_reason="Pipeline crash",
                dynamodb_client=ddb,
            )

        assert result is True
        call_kwargs = ddb.update_item.call_args[1]
        assert call_kwargs["Key"]["SK"]["S"] == "strands#SESSION"
        assert call_kwargs["ExpressionAttributeValues"][":state"]["S"] == "FAILED"
        assert call_kwargs["ExpressionAttributeValues"][":err"]["S"] == "Pipeline crash"

    def test_returns_false_on_error(self):
        ddb = _ddb_with_state("SCOPING")
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_failed("rca-123", dynamodb_client=ddb)

        assert result is False
