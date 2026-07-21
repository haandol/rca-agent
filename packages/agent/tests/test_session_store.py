from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from rca_agent.adapters.secondary.session.dynamodb_session_store import (
    DynamoDbSessionStore,
    SessionCancelledError,
    build_idempotency_key,
    build_rca_id,
    check_duplicate,
    create_session,
    mark_completed,
    mark_failed,
    update_state,
)
from rca_agent.ports.dto.models import (
    AlarmPayload,
    AlarmTrigger,
    FaultType,
    NotificationMessage,
    RcaSessionState,
    RemediationResult,
    VerificationResult,
)


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

    def test_completion_persists_selected_hypothesis_and_pending_notification(self):
        ddb = _ddb_with_state("REPORT_GENERATION")
        notification = NotificationMessage(
            rca_id="rca-123",
            root_cause_summary="database connection leak",
            severity="high",
            selected_hypothesis_id="h-selected",
            fault_type=FaultType.DB_CONNECTION_LEAK,
        )
        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_completed(
                "rca-123",
                root_cause="database connection leak",
                confirmed=True,
                selected_hypothesis_id="h-selected",
                fault_type=FaultType.DB_CONNECTION_LEAK,
                completion_notification=notification,
                dynamodb_client=ddb,
            )

        assert result is True
        values = ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":hid"]["S"] == "h-selected"
        assert values[":fault_type"]["S"] == FaultType.DB_CONNECTION_LEAK.value
        assert values[":notification_pending"]["S"] == "PENDING"
        assert NotificationMessage.model_validate_json(values[":notification"]["S"]) == notification

    def test_returns_false_on_error(self):
        ddb = _ddb_with_state("REPORT_GENERATION")
        error_response = {"Error": {"Code": "InternalServerError", "Message": "boom"}}
        ddb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        with patch("rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME", "rca-sessions"):
            result = mark_completed("rca-123", dynamodb_client=ddb)

        assert result is False


class TestRemediationPersistence:
    def test_loads_exact_selected_hypothesis_and_fault_type(self):
        ddb = MagicMock()
        ddb.get_item.side_effect = [
            {
                "Item": {
                    "state": {"S": "COMPLETED"},
                    "root_cause": {"S": "generated report wording"},
                    "confirmed": {"BOOL": True},
                    "selected_hypothesis_id": {"S": "h-selected"},
                    "fault_type": {"S": FaultType.DB_CONNECTION_LEAK.value},
                }
            },
            {
                "Item": {
                    "status": {"S": "CONFIRMED"},
                    "description": {"S": "exact selected database leak"},
                    "evidence_summary": {"S": "selected evidence only"},
                    "fault_type": {"S": FaultType.DB_CONNECTION_LEAK.value},
                }
            },
        ]

        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            context = DynamoDbSessionStore(ddb).get_remediation_context("rca-123")

        assert context is not None
        assert context.selected_hypothesis_id == "h-selected"
        assert context.validated_root_cause == "exact selected database leak"
        assert context.evidence_summary == "selected evidence only"
        assert context.fault_type == FaultType.DB_CONNECTION_LEAK
        exact_key = ddb.get_item.call_args_list[1].kwargs["Key"]
        assert exact_key["SK"]["S"] == "strands#HYPO#h-selected"
        ddb.query.assert_not_called()

    def test_loads_and_marks_completion_handoff(self):
        ddb = MagicMock()
        notification = NotificationMessage(
            rca_id="rca-123",
            root_cause_summary="completed",
            severity="high",
        )
        ddb.get_item.return_value = {
            "Item": {
                "state": {"S": "COMPLETED"},
                "completion_notification_status": {"S": "PENDING"},
                "completion_notification": {"S": notification.model_dump_json()},
            }
        }
        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            store = DynamoDbSessionStore(ddb)
            handoff = store.get_completion_handoff("rca-123")
            marked = store.mark_completion_notified("rca-123")

        assert handoff is not None
        assert handoff.notification_status == "PENDING"
        assert handoff.notification == notification
        assert marked is True
        update = ddb.update_item.call_args.kwargs
        assert "completion_notification_status = :sent" in update["UpdateExpression"]
        assert update["ExpressionAttributeValues"][":sent"]["S"] == "SENT"

    def test_legacy_session_without_exact_selection_fails_closed(self):
        ddb = MagicMock()
        ddb.get_item.return_value = {
            "Item": {
                "state": {"S": "COMPLETED"},
                "root_cause": {"S": "database connections exhausted"},
                "confirmed": {"BOOL": True},
            }
        }
        ddb.query.return_value = {
            "Items": [
                {
                    "description": {"S": "database connection pool leak"},
                    "evidence_summary": {"S": "connections remained checked out"},
                    "updated_at": {"S": "2026-07-21T01:00:00+00:00"},
                }
            ]
        }

        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            context = DynamoDbSessionStore(ddb).get_remediation_context("rca-123")

        assert context is not None
        assert context.state == RcaSessionState.COMPLETED
        assert context.root_cause == "database connections exhausted"
        assert context.validated_root_cause == ""
        assert context.evidence_summary == ""
        assert ddb.get_item.call_args.kwargs["ConsistentRead"] is True
        ddb.query.assert_not_called()

    def test_selected_hypothesis_must_be_confirmed_and_match_session_fault_type(self):
        ddb = MagicMock()
        session = {
            "Item": {
                "state": {"S": "COMPLETED"},
                "root_cause": {"S": "generated report wording"},
                "confirmed": {"BOOL": True},
                "selected_hypothesis_id": {"S": "h-selected"},
                "fault_type": {"S": FaultType.DB_CONNECTION_LEAK.value},
            }
        }
        ddb.get_item.side_effect = [
            session,
            {
                "Item": {
                    "status": {"S": "CONFIRMED"},
                    "description": {"S": "different structured action"},
                    "evidence_summary": {"S": "evidence"},
                    "fault_type": {"S": FaultType.HIGH_CPU.value},
                }
            },
        ]

        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            context = DynamoDbSessionStore(ddb).get_remediation_context("rca-123")

        assert context is not None
        assert context.validated_root_cause == ""
        assert context.evidence_summary == ""
        assert context.fault_type == FaultType.DB_CONNECTION_LEAK

    def test_claim_is_conditional_and_returns_token(self):
        ddb = MagicMock()
        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            token = DynamoDbSessionStore(ddb).claim_remediation("rca-123")

        assert token
        call = ddb.update_item.call_args.kwargs
        assert "#state = :completed" in call["ConditionExpression"]
        assert "remediation_status = :processing" in call["UpdateExpression"]
        assert call["ExpressionAttributeValues"][":token"]["S"] == token

    def test_duplicate_claim_returns_none(self):
        ddb = MagicMock()
        ddb.update_item.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ConditionalCheckFailedException",
                    "Message": "duplicate",
                }
            },
            "UpdateItem",
        )
        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            token = DynamoDbSessionStore(ddb).claim_remediation("rca-123")

        assert token is None

    def test_completion_is_guarded_by_claim_token(self):
        ddb = MagicMock()
        result = RemediationResult(
            rca_id="rca-123",
            overall_success=True,
            summary="reset complete",
        )
        verification = VerificationResult(
            rca_id="rca-123",
            metrics_normalized=False,
            verification_summary="connections remain above threshold",
            remaining_issues=["DatabaseConnections is still high"],
        )
        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            completed = DynamoDbSessionStore(ddb).complete_remediation(
                "rca-123",
                "claim-1",
                result,
                verification,
            )

        assert completed is True
        call = ddb.update_item.call_args.kwargs
        assert "remediation_claim_token = :token" in call["ConditionExpression"]
        assert call["ExpressionAttributeValues"][":token"]["S"] == "claim-1"
        values = call["ExpressionAttributeValues"]
        assert values[":verification_status"]["S"] == "FAILED"
        assert values[":metrics_normalized"]["BOOL"] is False
        assert values[":verification_summary"]["S"] == "connections remain above threshold"
        assert values[":verification_remaining_issues"]["L"] == [{"S": "DatabaseConnections is still high"}]

    def test_completion_persists_pending_when_verification_did_not_run(self):
        ddb = MagicMock()
        result = RemediationResult(
            rca_id="rca-123",
            overall_success=False,
            summary="no action executed",
        )
        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            completed = DynamoDbSessionStore(ddb).complete_remediation(
                "rca-123",
                "claim-1",
                result,
                None,
            )

        assert completed is True
        values = ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":verification_status"]["S"] == "PENDING"
        assert values[":metrics_normalized"]["BOOL"] is False
        assert values[":verification_summary"]["S"] == ""
        assert values[":verification_remaining_issues"]["L"] == []

    @mock_aws
    def test_claim_lifecycle_against_dynamodb(self):
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="rca-sessions",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.put_item(
            TableName="rca-sessions",
            Item={
                "PK": {"S": "RCA#rca-123"},
                "SK": {"S": "strands#SESSION"},
                "state": {"S": "COMPLETED"},
                "root_cause": {"S": "database connections exhausted"},
                "confirmed": {"BOOL": True},
                "selected_hypothesis_id": {"S": "h-1"},
                "fault_type": {"S": FaultType.DB_CONNECTION_LEAK.value},
            },
        )
        ddb.put_item(
            TableName="rca-sessions",
            Item={
                "PK": {"S": "RCA#rca-123"},
                "SK": {"S": "strands#HYPO#h-1"},
                "status": {"S": "CONFIRMED"},
                "description": {"S": "database connection pool leak"},
                "evidence_summary": {"S": "connections remained checked out"},
                "fault_type": {"S": FaultType.DB_CONNECTION_LEAK.value},
                "updated_at": {"S": "2026-07-21T01:00:00+00:00"},
            },
        )
        store = DynamoDbSessionStore(ddb)

        with patch(
            "rca_agent.adapters.secondary.session.dynamodb_session_store.DYNAMODB_TABLE_NAME",
            "rca-sessions",
        ):
            context = store.get_remediation_context("rca-123")
            token = store.claim_remediation("rca-123")
            duplicate_token = store.claim_remediation("rca-123")
            completed = store.complete_remediation(
                "rca-123",
                token or "",
                RemediationResult(
                    rca_id="rca-123",
                    overall_success=True,
                    summary="reset complete",
                ),
                VerificationResult(
                    rca_id="rca-123",
                    metrics_normalized=True,
                    verification_summary="connections normalized",
                ),
            )
            completed_context = store.get_remediation_context("rca-123")

        assert context is not None
        assert context.selected_hypothesis_id == "h-1"
        assert context.validated_root_cause == "database connection pool leak"
        assert context.fault_type == FaultType.DB_CONNECTION_LEAK
        assert token
        assert duplicate_token is None
        assert completed is True
        assert completed_context is not None
        assert completed_context.remediation_status == "COMPLETED"
        assert completed_context.verification_status == "NORMALIZED"
        assert completed_context.metrics_normalized is True
        assert completed_context.verification_summary == "connections normalized"
        assert completed_context.verification_remaining_issues == []


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
