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


def _get_current_state(
    rca_id: str,
    *,
    dynamodb_client=None,
) -> str | None:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return None
    resp = dynamodb_client.get_item(
        TableName=DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
        ProjectionExpression="#st",
        ExpressionAttributeNames={"#st": "state"},
    )
    item = resp.get("Item")
    return item["state"]["S"] if item else None


def _validate_transition(
    rca_id: str,
    target: str,
    *,
    dynamodb_client=None,
) -> None:
    current = _get_current_state(rca_id, dynamodb_client=dynamodb_client)
    if current is None:
        return
    if current in TERMINAL_STATES:
        raise SessionCancelledError(rca_id)
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidStateTransitionError(f"{rca_id}: {current} → {target}")


def create_session(
    alarm: AlarmPayload,
    *,
    dynamodb_client=None,
) -> RcaSession | None:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return None
    idempotency_key = build_idempotency_key(alarm)
    rca_id = build_rca_id(idempotency_key)
    now = datetime.now(UTC)
    ttl = int(time.time()) + SESSION_TTL_DAYS * 86400
    item = {
        "PK": {"S": f"RCA#{rca_id}"},
        "SK": {"S": f"{ENGINE}#SESSION"},
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
        dynamodb_client.put_item(
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


def check_duplicate(
    alarm: AlarmPayload,
    *,
    dynamodb_client=None,
) -> bool:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return False
    idempotency_key = build_idempotency_key(alarm)
    rca_id = build_rca_id(idempotency_key)
    try:
        response = dynamodb_client.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": f"{ENGINE}#SESSION"},
            },
        )
        if response.get("Item"):
            logger.info("Duplicate alarm found: idempotency_key=%s", idempotency_key)
            return True
    except ClientError:
        logger.exception("Failed to check duplicate, proceeding with processing")
    return False


def update_state(
    rca_id: str,
    new_state: RcaSessionState,
    *,
    dynamodb_client=None,
) -> bool:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return False
    _validate_transition(rca_id, new_state.value, dynamodb_client=dynamodb_client)
    now = datetime.now(UTC).isoformat()
    try:
        dynamodb_client.update_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": f"{ENGINE}#SESSION"},
            },
            UpdateExpression="SET #st = :state, updated_at = :now",
            ConditionExpression="attribute_exists(SK) AND #st <> :cancelled",
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues={
                ":state": {"S": new_state.value},
                ":now": {"S": now},
                ":cancelled": {"S": RcaSessionState.CANCELLED.value},
            },
        )
        logger.info("Session %s state updated to %s", rca_id, new_state.value)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("Session %s has been cancelled, aborting pipeline", rca_id)
            raise SessionCancelledError(rca_id) from e
        logger.exception("Failed to update session state for %s", rca_id)
        return False


def mark_completed(
    rca_id: str,
    *,
    root_cause: str = "",
    confirmed: bool = False,
    dynamodb_client=None,
) -> bool:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return False
    _validate_transition(rca_id, "COMPLETED", dynamodb_client=dynamodb_client)
    now = datetime.now(UTC).isoformat()
    try:
        dynamodb_client.update_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": f"{ENGINE}#SESSION"},
            },
            UpdateExpression="SET #st = :state, updated_at = :now, root_cause = :rc, confirmed = :cf",
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues={
                ":state": {"S": RcaSessionState.COMPLETED.value},
                ":now": {"S": now},
                ":rc": {"S": root_cause},
                ":cf": {"BOOL": confirmed},
            },
        )
        logger.info("Session %s marked COMPLETED", rca_id)
        return True
    except ClientError:
        logger.exception("Failed to mark session %s as completed", rca_id)
        return False


def mark_outdated(
    rca_id: str,
    *,
    reason: str = "",
    dynamodb_client=None,
) -> bool:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return False
    _validate_transition(rca_id, "OUTDATED", dynamodb_client=dynamodb_client)
    now = datetime.now(UTC).isoformat()
    try:
        dynamodb_client.update_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": f"{ENGINE}#SESSION"},
            },
            UpdateExpression="SET #st = :state, updated_at = :now, error_reason = :reason",
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues={
                ":state": {"S": RcaSessionState.OUTDATED.value},
                ":now": {"S": now},
                ":reason": {"S": reason},
            },
        )
        logger.info("Session %s marked OUTDATED: %s", rca_id, reason)
        return True
    except ClientError:
        logger.exception("Failed to mark session %s as outdated", rca_id)
        return False


def mark_failed(
    rca_id: str,
    *,
    error_reason: str = "",
    dynamodb_client=None,
) -> bool:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return False
    _validate_transition(rca_id, "FAILED", dynamodb_client=dynamodb_client)
    now = datetime.now(UTC).isoformat()
    try:
        dynamodb_client.update_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": f"{ENGINE}#SESSION"},
            },
            UpdateExpression="SET #st = :state, updated_at = :now, error_reason = :err",
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues={
                ":state": {"S": RcaSessionState.FAILED.value},
                ":now": {"S": now},
                ":err": {"S": error_reason},
            },
        )
        logger.info("Session %s marked FAILED: %s", rca_id, error_reason)
        return True
    except ClientError:
        logger.exception("Failed to mark session %s as failed", rca_id)
        return False


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
            Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
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

    def check_duplicate(self, alarm: AlarmPayload) -> bool:
        if not self._enabled:
            return False
        idempotency_key = build_idempotency_key(alarm)
        rca_id = build_rca_id(idempotency_key)
        try:
            response = self._dynamodb.get_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={
                    "PK": {"S": f"RCA#{rca_id}"},
                    "SK": {"S": f"{ENGINE}#SESSION"},
                },
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
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": f"{ENGINE}#SESSION"},
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
        if not self._enabled:
            return False
        self._validate_transition(rca_id, new_state.value)
        now = datetime.now(UTC).isoformat()
        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={
                    "PK": {"S": f"RCA#{rca_id}"},
                    "SK": {"S": f"{ENGINE}#SESSION"},
                },
                UpdateExpression="SET #st = :state, updated_at = :now",
                ConditionExpression="attribute_exists(SK) AND #st <> :cancelled",
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={
                    ":state": {"S": new_state.value},
                    ":now": {"S": now},
                    ":cancelled": {"S": RcaSessionState.CANCELLED.value},
                },
            )
            logger.info("Session %s state updated to %s", rca_id, new_state.value)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info("Session %s has been cancelled, aborting pipeline", rca_id)
                raise SessionCancelledError(rca_id) from e
            logger.exception("Failed to update session state for %s", rca_id)
            return False

    def mark_completed(self, rca_id: str, *, root_cause: str = "", confirmed: bool = False) -> bool:
        if not self._enabled:
            return False
        self._validate_transition(rca_id, "COMPLETED")
        now = datetime.now(UTC).isoformat()
        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={
                    "PK": {"S": f"RCA#{rca_id}"},
                    "SK": {"S": f"{ENGINE}#SESSION"},
                },
                UpdateExpression="SET #st = :state, updated_at = :now, root_cause = :rc, confirmed = :cf",
                ConditionExpression="attribute_exists(SK)",
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={
                    ":state": {"S": RcaSessionState.COMPLETED.value},
                    ":now": {"S": now},
                    ":rc": {"S": root_cause},
                    ":cf": {"BOOL": confirmed},
                },
            )
            logger.info("Session %s marked COMPLETED", rca_id)
            return True
        except ClientError:
            logger.exception("Failed to mark session %s as completed", rca_id)
            return False

    def mark_failed(self, rca_id: str, *, error_reason: str = "") -> bool:
        if not self._enabled:
            return False
        self._validate_transition(rca_id, "FAILED")
        now = datetime.now(UTC).isoformat()
        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={
                    "PK": {"S": f"RCA#{rca_id}"},
                    "SK": {"S": f"{ENGINE}#SESSION"},
                },
                UpdateExpression="SET #st = :state, updated_at = :now, error_reason = :err",
                ConditionExpression="attribute_exists(SK)",
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={
                    ":state": {"S": RcaSessionState.FAILED.value},
                    ":now": {"S": now},
                    ":err": {"S": error_reason},
                },
            )
            logger.info("Session %s marked FAILED: %s", rca_id, error_reason)
            return True
        except ClientError:
            logger.exception("Failed to mark session %s as failed", rca_id)
            return False

    def mark_outdated(self, rca_id: str, *, reason: str = "") -> bool:
        if not self._enabled:
            return False
        self._validate_transition(rca_id, "OUTDATED")
        now = datetime.now(UTC).isoformat()
        try:
            self._dynamodb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={
                    "PK": {"S": f"RCA#{rca_id}"},
                    "SK": {"S": f"{ENGINE}#SESSION"},
                },
                UpdateExpression="SET #st = :state, updated_at = :now, error_reason = :reason",
                ConditionExpression="attribute_exists(SK)",
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={
                    ":state": {"S": RcaSessionState.OUTDATED.value},
                    ":now": {"S": now},
                    ":reason": {"S": reason},
                },
            )
            logger.info("Session %s marked OUTDATED: %s", rca_id, reason)
            return True
        except ClientError:
            logger.exception("Failed to mark session %s as outdated", rca_id)
            return False
