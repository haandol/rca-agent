from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from rca_agent.config.settings import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS
from rca_agent.ports.dto.models import (
    AlarmPayload,
    CompletionHandoff,
    FaultType,
    NotificationMessage,
    RcaSession,
    RcaSessionState,
)
from rca_agent.ports.interfaces.session_store import (
    ClaimDisposition,
    SessionClaim,
    SessionOwnershipCheckError,
    SessionStorePort,
    SideEffectLeaseUnavailableError,
)

logger = logging.getLogger(__name__)


def build_idempotency_key(alarm: AlarmPayload) -> str:
    ts = alarm.state_change_time.isoformat() if alarm.state_change_time else "unknown"
    return f"{alarm.alarm_name}#{ts}"


def build_rca_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))


def _resolve_message_id(alarm: AlarmPayload, message_id: str | None) -> str:
    if message_id:
        return message_id
    return f"direct:{build_rca_id(build_idempotency_key(alarm))}"


def _session_key(rca_id: str) -> dict:
    return {"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}}


class SessionCancelledError(Exception):
    pass


class InvalidStateTransitionError(Exception):
    pass


VALID_TRANSITIONS: dict[str, set[str]] = {
    "ALARM_RECEIVED": {"SCOPING", "FAILED", "OUTDATED", "CANCELLED"},
    "SCOPING": {"HYPOTHESIS_GENERATION", "FAILED", "OUTDATED", "CANCELLED"},
    "HYPOTHESIS_GENERATION": {"HYPOTHESIS_PRIORITIZATION", "FAILED", "OUTDATED", "CANCELLED"},
    "HYPOTHESIS_PRIORITIZATION": {"EVIDENCE_COLLECTION", "FAILED", "OUTDATED", "CANCELLED"},
    "EVIDENCE_COLLECTION": {"HYPOTHESIS_VALIDATION", "FAILED", "OUTDATED", "CANCELLED"},
    "HYPOTHESIS_VALIDATION": {
        "REPORT_GENERATION",
        "HYPOTHESIS_PRIORITIZATION",
        "EVIDENCE_COLLECTION",
        "HYPOTHESIS_GENERATION",
        "FAILED",
        "OUTDATED",
        "CANCELLED",
    },
    "REPORT_GENERATION": {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"},
}

TERMINAL_STATES = {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"}
_TERMINAL_STATES = TERMINAL_STATES
_DEDUPE_STATES = {"COMPLETED", "OUTDATED", "CANCELLED"}
_RECLAIMABLE_STATES = {
    "ALARM_RECEIVED",
    "SCOPING",
    "HYPOTHESIS_GENERATION",
    "HYPOTHESIS_PRIORITIZATION",
    "EVIDENCE_COLLECTION",
    "HYPOTHESIS_VALIDATION",
    "REPORT_GENERATION",
    "FAILED",
}
_ACTIVE_CLAIM_CONDITION = (
    "attribute_exists(SK) AND claim_token = :claim AND NOT #st IN (:completed, :failed, :outdated, :cancelled)"
)
_EXPECTED_STATE_CLAIM_CONDITION = "attribute_exists(SK) AND claim_token = :claim AND #st = :expected_state"
_AVAILABLE_SIDE_EFFECT_LEASE_CONDITION = (
    "((attribute_not_exists(side_effect_lease_token) "
    "AND attribute_not_exists(side_effect_lease_expires_at)) "
    "OR (attribute_exists(side_effect_lease_token) "
    "AND side_effect_lease_expires_at <= :now_epoch))"
)


class DynamoDbSessionStore(SessionStorePort):
    def __init__(self, dynamodb_client=None):
        self._dynamodb = dynamodb_client

    @property
    def _enabled(self) -> bool:
        return bool(DYNAMODB_TABLE_NAME and self._dynamodb)

    def _get_current_state(self, rca_id: str) -> str | None:
        item = self._get_session(rca_id)
        return item["state"]["S"] if item else None

    def _get_session(self, rca_id: str) -> dict | None:
        if not self._enabled:
            return None
        resp = self._dynamodb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key=_session_key(rca_id),
            ConsistentRead=True,
        )
        return resp.get("Item")

    def _validate_transition(
        self,
        rca_id: str,
        target: str,
        claim_token: str | None,
    ) -> str:
        if not claim_token:
            raise SessionOwnershipCheckError(f"{rca_id}: claim token is required")
        try:
            item = self._get_session(rca_id)
        except Exception as exc:
            raise SessionOwnershipCheckError(rca_id) from exc
        if item is None:
            raise SessionOwnershipCheckError(f"{rca_id}: session item is missing")
        if item.get("claim_token", {}).get("S") != claim_token:
            raise SessionCancelledError(rca_id)
        current = item.get("state", {}).get("S", "")
        if current in TERMINAL_STATES:
            raise SessionCancelledError(rca_id)
        allowed = VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidStateTransitionError(f"{rca_id}: {current} → {target}")
        return current

    def _update_with_state(
        self,
        rca_id: str,
        *,
        target_state: str,
        claim_token: str | None,
        extra_sets: dict[str, tuple[str, dict]] | None = None,
        log_success: str | None = None,
        error_log: str = "Failed to update session %s",
    ) -> bool:
        """Shared `update_item` with state + updated_at + optional extra fields.

        `extra_sets` maps attribute-name → (placeholder, value-dict). For example
        ``{"root_cause": (":rc", {"S": "..."})}`` adds ``root_cause = :rc`` to the
        SET clause with the provided value.
        """
        if not self._enabled:
            return False
        expected_state = self._validate_transition(
            rca_id,
            target_state,
            claim_token,
        )
        now = datetime.now(UTC).isoformat()

        set_parts = ["#st = :state", "updated_at = :now"]
        attr_values: dict = {
            ":state": {"S": target_state},
            ":expected_state": {"S": expected_state},
            ":now": {"S": now},
            ":claim": {"S": claim_token},
        }

        if extra_sets:
            for attr_name, (placeholder, value) in extra_sets.items():
                set_parts.append(f"{attr_name} = {placeholder}")
                attr_values[placeholder] = value

        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_session_key(rca_id),
                UpdateExpression="SET " + ", ".join(set_parts),
                ConditionExpression=_EXPECTED_STATE_CLAIM_CONDITION,
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues=attr_values,
            )
            if log_success:
                logger.info(log_success, rca_id)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info("Session %s claim is no longer current", rca_id)
                raise SessionCancelledError(rca_id) from e
            logger.exception(error_log, rca_id)
            raise SessionOwnershipCheckError(rca_id) from e

    def check_duplicate(self, alarm: AlarmPayload) -> bool:
        if not self._enabled:
            return False
        idempotency_key = build_idempotency_key(alarm)
        rca_id = build_rca_id(idempotency_key)
        try:
            response = self._dynamodb.get_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_session_key(rca_id),
            )
            if response.get("Item"):
                logger.info("Duplicate alarm found: idempotency_key=%s", idempotency_key)
                return True
        except ClientError:
            logger.exception("Failed to check duplicate, proceeding with processing")
        return False

    def create_session(self, alarm: AlarmPayload) -> RcaSession | None:
        message_id = _resolve_message_id(alarm, None)
        claim = self.claim_session(alarm, receive_count=1, message_id=message_id)
        if not claim.acquired or not claim.claim_token:
            return None
        idempotency_key = build_idempotency_key(alarm)
        rca_id = build_rca_id(idempotency_key)
        now = datetime.now(UTC)
        ttl = int(time.time()) + SESSION_TTL_DAYS * 86400
        return RcaSession(
            rca_id=rca_id,
            idempotency_key=idempotency_key,
            state=RcaSessionState.ALARM_RECEIVED,
            alarm_name=alarm.alarm_name,
            alarm_arn=alarm.alarm_arn or "",
            engine=ENGINE,
            created_at=now,
            updated_at=now,
            ttl=ttl,
            claim_token=claim.claim_token,
            receive_count=claim.attempt or 1,
            message_id=message_id,
        )

    def claim_session(
        self,
        alarm: AlarmPayload,
        *,
        receive_count: int,
        message_id: str | None = None,
    ) -> SessionClaim:
        if not self._enabled:
            return SessionClaim(ClaimDisposition.CONTENDED)
        receive_count = max(receive_count, 1)
        message_id = _resolve_message_id(alarm, message_id)
        claim_token = uuid.uuid4().hex
        idempotency_key = build_idempotency_key(alarm)
        rca_id = build_rca_id(idempotency_key)
        now = datetime.now(UTC)
        ttl = int(time.time()) + SESSION_TTL_DAYS * 86400
        item = {
            **_session_key(rca_id),
            "rca_id": {"S": rca_id},
            "engine": {"S": ENGINE},
            "idempotency_key": {"S": idempotency_key},
            "state": {"S": RcaSessionState.ALARM_RECEIVED.value},
            "alarm_name": {"S": alarm.alarm_name},
            "alarm_arn": {"S": alarm.alarm_arn or ""},
            "region": {"S": alarm.region},
            "claim_token": {"S": claim_token},
            "receive_count": {"N": str(receive_count)},
            "message_id": {"S": message_id},
            "created_at": {"S": now.isoformat()},
            "updated_at": {"S": now.isoformat()},
            "ttl": {"N": str(ttl)},
        }
        try:
            self._dynamodb.put_item(
                TableName=DYNAMODB_TABLE_NAME,
                Item=item,
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                pass
            else:
                raise
        else:
            logger.info("RCA session claimed: rca_id=%s, receive_count=%d", rca_id, receive_count)
            return SessionClaim(ClaimDisposition.CLAIMED, claim_token, receive_count)

        try:
            existing = self._get_session(rca_id)
        except Exception as exc:
            raise SessionOwnershipCheckError(rca_id) from exc
        if not existing:
            return SessionClaim(ClaimDisposition.CONTENDED)

        state = existing.get("state", {}).get("S", "")
        previous_claim = existing.get("claim_token", {}).get("S")
        previous_message_id_attr = existing.get("message_id")
        previous_message_id = previous_message_id_attr.get("S") if previous_message_id_attr else None
        previous_receive_count_attr = existing.get("receive_count")
        try:
            previous_receive_count = (
                int(previous_receive_count_attr.get("N", "0")) if previous_receive_count_attr else 0
            )
        except (TypeError, ValueError):
            return SessionClaim(ClaimDisposition.CONTENDED)

        if state in _DEDUPE_STATES:
            return SessionClaim(
                ClaimDisposition.TERMINAL_DUPLICATE,
                previous_claim,
                previous_receive_count or None,
            )
        if state not in _RECLAIMABLE_STATES or receive_count <= previous_receive_count:
            return SessionClaim(ClaimDisposition.CONTENDED)
        if previous_message_id_attr and not previous_message_id:
            return SessionClaim(ClaimDisposition.CONTENDED)
        if previous_message_id is not None and previous_message_id != message_id:
            return SessionClaim(ClaimDisposition.CONTENDED)

        now_epoch = int(time.time())
        condition = f"#st = :previous_state AND {_AVAILABLE_SIDE_EFFECT_LEASE_CONDITION}"
        expression_values = {
            ":previous_state": {"S": state},
            ":now_epoch": {"N": str(now_epoch)},
        }
        if previous_receive_count_attr:
            condition += " AND receive_count = :previous_receive_count"
            expression_values[":previous_receive_count"] = {"N": str(previous_receive_count)}
        else:
            condition += " AND attribute_not_exists(receive_count)"
        if previous_claim:
            condition += " AND claim_token = :previous_claim"
            expression_values[":previous_claim"] = {"S": previous_claim}
        else:
            condition += " AND attribute_not_exists(claim_token)"
        if previous_message_id_attr:
            condition += " AND message_id = :previous_message_id"
            expression_values[":previous_message_id"] = {"S": previous_message_id}
        else:
            condition += " AND attribute_not_exists(message_id)"

        try:
            self._dynamodb.put_item(
                TableName=DYNAMODB_TABLE_NAME,
                Item=item,
                ConditionExpression=condition,
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues=expression_values,
            )
            logger.info("RCA session reclaimed: rca_id=%s, receive_count=%d", rca_id, receive_count)
            return SessionClaim(ClaimDisposition.CLAIMED, claim_token, receive_count)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return SessionClaim(ClaimDisposition.CONTENDED)
            raise SessionOwnershipCheckError(rca_id) from exc

    def acquire_side_effect_lease(
        self,
        rca_id: str,
        claim_token: str,
        effect_name: str,
        *,
        lease_seconds: int,
    ) -> str:
        if not self._enabled or not claim_token:
            raise SideEffectLeaseUnavailableError(rca_id)
        lease_token = uuid.uuid4().hex
        now = int(time.time())
        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_session_key(rca_id),
                UpdateExpression=(
                    "SET side_effect_lease_token = :lease, "
                    "side_effect_lease_claim_token = :claim, "
                    "side_effect_lease_name = :effect, "
                    "side_effect_lease_expires_at = :expires"
                ),
                ConditionExpression=(_ACTIVE_CLAIM_CONDITION + " AND " + _AVAILABLE_SIDE_EFFECT_LEASE_CONDITION),
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={
                    ":claim": {"S": claim_token},
                    ":lease": {"S": lease_token},
                    ":effect": {"S": effect_name},
                    ":expires": {"N": str(now + max(lease_seconds, 1))},
                    ":now_epoch": {"N": str(now)},
                    ":completed": {"S": RcaSessionState.COMPLETED.value},
                    ":failed": {"S": RcaSessionState.FAILED.value},
                    ":outdated": {"S": RcaSessionState.OUTDATED.value},
                    ":cancelled": {"S": RcaSessionState.CANCELLED.value},
                },
            )
        except ClientError as exc:
            raise SideEffectLeaseUnavailableError(rca_id) from exc
        return lease_token

    def release_side_effect_lease(
        self,
        rca_id: str,
        claim_token: str,
        lease_token: str,
    ) -> bool:
        if not self._enabled or not claim_token or not lease_token:
            return False
        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_session_key(rca_id),
                UpdateExpression=(
                    "REMOVE side_effect_lease_token, side_effect_lease_claim_token, "
                    "side_effect_lease_name, side_effect_lease_expires_at"
                ),
                ConditionExpression=(
                    "claim_token = :claim AND "
                    "side_effect_lease_claim_token = :claim AND "
                    "side_effect_lease_token = :lease"
                ),
                ExpressionAttributeValues={
                    ":claim": {"S": claim_token},
                    ":lease": {"S": lease_token},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise SideEffectLeaseUnavailableError(rca_id) from exc

    def update_state(
        self,
        rca_id: str,
        new_state: RcaSessionState,
        *,
        claim_token: str | None = None,
    ) -> bool:
        return self._update_with_state(
            rca_id,
            target_state=new_state.value,
            claim_token=claim_token,
            log_success=f"Session %s state updated to {new_state.value}",
            error_log="Failed to update session state for %s",
        )

    def mark_completed(
        self,
        rca_id: str,
        *,
        root_cause: str = "",
        confirmed: bool = False,
        selected_hypothesis_id: str = "",
        fault_type: FaultType = FaultType.UNSUPPORTED,
        completion_notification: NotificationMessage | None = None,
        report_s3_key: str = "",
        claim_token: str | None = None,
    ) -> bool:
        extra_sets = {
            "root_cause": (":rc", {"S": root_cause}),
            "confirmed": (":cf", {"BOOL": confirmed}),
            "selected_hypothesis_id": (":hid", {"S": selected_hypothesis_id}),
            "fault_type": (":fault_type", {"S": fault_type.value}),
            "report_s3_key": (":report_s3_key", {"S": report_s3_key}),
        }
        if completion_notification is not None:
            extra_sets.update(
                {
                    "completion_notification_status": (":notification_pending", {"S": "PENDING"}),
                    "completion_notification": (
                        ":notification",
                        {"S": completion_notification.model_dump_json()},
                    ),
                }
            )
        return self._update_with_state(
            rca_id,
            target_state=RcaSessionState.COMPLETED.value,
            claim_token=claim_token,
            extra_sets=extra_sets,
            log_success="Session %s marked COMPLETED",
            error_log="Failed to mark session %s as completed",
        )

    def mark_failed(
        self,
        rca_id: str,
        *,
        error_reason: str = "",
        claim_token: str | None = None,
    ) -> bool:
        ok = self._update_with_state(
            rca_id,
            target_state=RcaSessionState.FAILED.value,
            claim_token=claim_token,
            extra_sets={"error_reason": (":err", {"S": error_reason})},
            error_log="Failed to mark session %s as failed",
        )
        if ok:
            logger.info("Session %s marked FAILED: %s", rca_id, error_reason)
        return ok

    def mark_outdated(
        self,
        rca_id: str,
        *,
        reason: str = "",
        claim_token: str | None = None,
    ) -> bool:
        ok = self._update_with_state(
            rca_id,
            target_state=RcaSessionState.OUTDATED.value,
            claim_token=claim_token,
            extra_sets={"error_reason": (":reason", {"S": reason})},
            error_log="Failed to mark session %s as outdated",
        )
        if ok:
            logger.info("Session %s marked OUTDATED: %s", rca_id, reason)
        return ok

    def get_completion_handoff(self, rca_id: str) -> CompletionHandoff | None:
        if not self._enabled:
            return None
        response = self._dynamodb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key=_session_key(rca_id),
            ConsistentRead=True,
            ProjectionExpression="#state, completion_notification_status, completion_notification",
            ExpressionAttributeNames={"#state": "state"},
        )
        item = response.get("Item")
        if not item:
            return None

        notification = None
        raw_notification = item.get("completion_notification", {}).get("S", "")
        if raw_notification:
            try:
                notification = NotificationMessage.model_validate_json(raw_notification)
            except Exception:
                logger.exception("Invalid completion notification persisted for %s", rca_id)
        return CompletionHandoff(
            rca_id=rca_id,
            state=item.get("state", {}).get("S", RcaSessionState.FAILED.value),
            notification_status=item.get("completion_notification_status", {}).get("S", ""),
            notification=notification,
        )

    def mark_completion_notified(self, rca_id: str, *, claim_token: str | None = None) -> bool:
        if not self._enabled or not claim_token:
            return False
        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_session_key(rca_id),
                UpdateExpression=("SET completion_notification_status = :sent, completion_notified_at = :now"),
                ConditionExpression=(
                    "#state = :completed AND claim_token = :claim AND completion_notification_status = :pending"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":completed": {"S": RcaSessionState.COMPLETED.value},
                    ":pending": {"S": "PENDING"},
                    ":claim": {"S": claim_token},
                    ":sent": {"S": "SENT"},
                    ":now": {"S": datetime.now(UTC).isoformat()},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


# ── Module-level function API (delegates to DynamoDbSessionStore) ──────────────


def _validate_transition(
    rca_id: str,
    target: str,
    *,
    claim_token: str | None = None,
    dynamodb_client=None,
) -> str:
    return DynamoDbSessionStore(dynamodb_client)._validate_transition(
        rca_id,
        target,
        claim_token,
    )


def _get_current_state(rca_id: str, *, dynamodb_client=None) -> str | None:
    return DynamoDbSessionStore(dynamodb_client)._get_current_state(rca_id)


def create_session(alarm: AlarmPayload, *, dynamodb_client=None) -> RcaSession | None:
    return DynamoDbSessionStore(dynamodb_client).create_session(alarm)


def check_duplicate(alarm: AlarmPayload, *, dynamodb_client=None) -> bool:
    return DynamoDbSessionStore(dynamodb_client).check_duplicate(alarm)


def claim_session(
    alarm: AlarmPayload,
    *,
    receive_count: int,
    message_id: str | None = None,
    dynamodb_client=None,
) -> SessionClaim:
    return DynamoDbSessionStore(dynamodb_client).claim_session(
        alarm,
        receive_count=receive_count,
        message_id=message_id,
    )


def update_state(
    rca_id: str,
    new_state: RcaSessionState,
    *,
    claim_token: str | None = None,
    dynamodb_client=None,
) -> bool:
    return DynamoDbSessionStore(dynamodb_client).update_state(
        rca_id,
        new_state,
        claim_token=claim_token,
    )


def mark_completed(
    rca_id: str,
    *,
    root_cause: str = "",
    confirmed: bool = False,
    selected_hypothesis_id: str = "",
    fault_type: FaultType = FaultType.UNSUPPORTED,
    completion_notification: NotificationMessage | None = None,
    report_s3_key: str = "",
    claim_token: str | None = None,
    dynamodb_client=None,
) -> bool:
    return DynamoDbSessionStore(dynamodb_client).mark_completed(
        rca_id,
        root_cause=root_cause,
        confirmed=confirmed,
        selected_hypothesis_id=selected_hypothesis_id,
        fault_type=fault_type,
        completion_notification=completion_notification,
        report_s3_key=report_s3_key,
        claim_token=claim_token,
    )


def mark_outdated(
    rca_id: str,
    *,
    reason: str = "",
    claim_token: str | None = None,
    dynamodb_client=None,
) -> bool:
    return DynamoDbSessionStore(dynamodb_client).mark_outdated(
        rca_id,
        reason=reason,
        claim_token=claim_token,
    )


def mark_failed(
    rca_id: str,
    *,
    error_reason: str = "",
    claim_token: str | None = None,
    dynamodb_client=None,
) -> bool:
    return DynamoDbSessionStore(dynamodb_client).mark_failed(
        rca_id,
        error_reason=error_reason,
        claim_token=claim_token,
    )
