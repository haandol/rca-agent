from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from rca_agent.config.settings import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS
from rca_agent.ports.dto.models import AlarmPayload, RcaSession, RcaSessionState
from rca_agent.ports.interfaces.session_store import SessionStorePort

logger = logging.getLogger(__name__)


def build_idempotency_key(alarm: AlarmPayload) -> str:
    ts = alarm.state_change_time.isoformat() if alarm.state_change_time else "unknown"
    return f"{alarm.alarm_name}#{ts}"


def build_rca_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))


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


class DynamoDbSessionStore(SessionStorePort):
    def __init__(self, dynamodb_client=None):
        self._dynamodb = dynamodb_client

    @property
    def _enabled(self) -> bool:
        return bool(DYNAMODB_TABLE_NAME and self._dynamodb)

    def _get_current_state(self, rca_id: str) -> str | None:
        if not self._enabled:
            return None
        resp = self._dynamodb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key=_session_key(rca_id),
            ProjectionExpression="#st",
            ExpressionAttributeNames={"#st": "state"},
        )
        item = resp.get("Item")
        return item["state"]["S"] if item else None

    def _validate_transition(self, rca_id: str, target: str) -> None:
        current = self._get_current_state(rca_id)
        if current is None:
            return
        if current in TERMINAL_STATES:
            raise SessionCancelledError(rca_id)
        allowed = VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidStateTransitionError(f"{rca_id}: {current} → {target}")

    def _update_with_state(
        self,
        rca_id: str,
        *,
        target_state: str,
        extra_sets: dict[str, tuple[str, dict]] | None = None,
        condition_expression: str = "attribute_exists(SK)",
        raise_cancelled_on_condition_fail: bool = False,
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
        self._validate_transition(rca_id, target_state)
        now = datetime.now(UTC).isoformat()

        set_parts = ["#st = :state", "updated_at = :now"]
        attr_values: dict = {
            ":state": {"S": target_state},
            ":now": {"S": now},
        }
        if raise_cancelled_on_condition_fail:
            attr_values[":cancelled"] = {"S": RcaSessionState.CANCELLED.value}

        if extra_sets:
            for attr_name, (placeholder, value) in extra_sets.items():
                set_parts.append(f"{attr_name} = {placeholder}")
                attr_values[placeholder] = value

        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_session_key(rca_id),
                UpdateExpression="SET " + ", ".join(set_parts),
                ConditionExpression=condition_expression,
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues=attr_values,
            )
            if log_success:
                logger.info(log_success, rca_id)
            return True
        except ClientError as e:
            if raise_cancelled_on_condition_fail and e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info("Session %s has been cancelled, aborting pipeline", rca_id)
                raise SessionCancelledError(rca_id) from e
            logger.exception(error_log, rca_id)
            return False

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
        if not self._enabled:
            return None
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
                logger.warning("Duplicate alarm detected (idempotency_key=%s), skipping", idempotency_key)
                return None
            raise
        logger.info("RCA session created: rca_id=%s, idempotency_key=%s", rca_id, idempotency_key)
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
        )

    def update_state(self, rca_id: str, new_state: RcaSessionState) -> bool:
        return self._update_with_state(
            rca_id,
            target_state=new_state.value,
            condition_expression="attribute_exists(SK) AND #st <> :cancelled",
            raise_cancelled_on_condition_fail=True,
            log_success=f"Session %s state updated to {new_state.value}",
            error_log="Failed to update session state for %s",
        )

    def mark_completed(self, rca_id: str, *, root_cause: str = "", confirmed: bool = False) -> bool:
        return self._update_with_state(
            rca_id,
            target_state=RcaSessionState.COMPLETED.value,
            extra_sets={
                "root_cause": (":rc", {"S": root_cause}),
                "confirmed": (":cf", {"BOOL": confirmed}),
            },
            log_success="Session %s marked COMPLETED",
            error_log="Failed to mark session %s as completed",
        )

    def mark_failed(self, rca_id: str, *, error_reason: str = "") -> bool:
        ok = self._update_with_state(
            rca_id,
            target_state=RcaSessionState.FAILED.value,
            extra_sets={"error_reason": (":err", {"S": error_reason})},
            error_log="Failed to mark session %s as failed",
        )
        if ok:
            logger.info("Session %s marked FAILED: %s", rca_id, error_reason)
        return ok

    def mark_outdated(self, rca_id: str, *, reason: str = "") -> bool:
        ok = self._update_with_state(
            rca_id,
            target_state=RcaSessionState.OUTDATED.value,
            extra_sets={"error_reason": (":reason", {"S": reason})},
            error_log="Failed to mark session %s as outdated",
        )
        if ok:
            logger.info("Session %s marked OUTDATED: %s", rca_id, reason)
        return ok


# ── Module-level function API (delegates to DynamoDbSessionStore) ──────────────


def _validate_transition(rca_id: str, target: str, *, dynamodb_client=None) -> None:
    DynamoDbSessionStore(dynamodb_client)._validate_transition(rca_id, target)


def _get_current_state(rca_id: str, *, dynamodb_client=None) -> str | None:
    return DynamoDbSessionStore(dynamodb_client)._get_current_state(rca_id)


def create_session(alarm: AlarmPayload, *, dynamodb_client=None) -> RcaSession | None:
    return DynamoDbSessionStore(dynamodb_client).create_session(alarm)


def check_duplicate(alarm: AlarmPayload, *, dynamodb_client=None) -> bool:
    return DynamoDbSessionStore(dynamodb_client).check_duplicate(alarm)


def update_state(rca_id: str, new_state: RcaSessionState, *, dynamodb_client=None) -> bool:
    return DynamoDbSessionStore(dynamodb_client).update_state(rca_id, new_state)


def mark_completed(
    rca_id: str,
    *,
    root_cause: str = "",
    confirmed: bool = False,
    dynamodb_client=None,
) -> bool:
    return DynamoDbSessionStore(dynamodb_client).mark_completed(rca_id, root_cause=root_cause, confirmed=confirmed)


def mark_outdated(rca_id: str, *, reason: str = "", dynamodb_client=None) -> bool:
    return DynamoDbSessionStore(dynamodb_client).mark_outdated(rca_id, reason=reason)


def mark_failed(rca_id: str, *, error_reason: str = "", dynamodb_client=None) -> bool:
    return DynamoDbSessionStore(dynamodb_client).mark_failed(rca_id, error_reason=error_reason)
