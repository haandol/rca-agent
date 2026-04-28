from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import structlog
from botocore.exceptions import ClientError

from cc_headless.config.settings import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS
from cc_headless.ports.interfaces.session_store import SessionStorePort

logger = structlog.get_logger()

_TERMINAL_COND = "attribute_exists(SK) AND NOT #state IN (:completed, :failed, :outdated, :cancelled)"


def build_rca_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))


class SessionCancelledError(Exception):
    pass


class InvalidStateTransitionError(Exception):
    pass


VALID_TRANSITIONS: dict[str, set[str]] = {
    "ALARM_RECEIVED": {"ANALYZING", "FAILED", "OUTDATED", "CANCELLED"},
    "ANALYZING": {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"},
}

_TERMINAL_STATES = {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ttl() -> str:
    return str(int(time.time()) + SESSION_TTL_DAYS * 86400)


class DynamoDbSessionStore(SessionStorePort):
    def __init__(self, dynamodb_client=None):
        self._ddb = dynamodb_client

    def _get_current_state(self, rca_id: str) -> str | None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return None
        resp = self._ddb.get_item(
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
        if current in _TERMINAL_STATES:
            raise SessionCancelledError(rca_id)
        allowed = VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidStateTransitionError(f"{rca_id}: {current} → {target}")

    def check_duplicate(self, rca_id: str) -> bool:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return False
        resp = self._ddb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
        )
        return "Item" in resp

    def create_session(
        self,
        rca_id: str,
        alarm_name: str,
        idempotency_key: str,
        *,
        alarm_data: dict | None = None,
    ) -> bool:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return True
        now = _now_iso()
        ttl = _ttl()
        item = {
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": f"{ENGINE}#SESSION"},
            "rca_id": {"S": rca_id},
            "idempotency_key": {"S": idempotency_key},
            "alarm_name": {"S": alarm_name},
            "state": {"S": "ALARM_RECEIVED"},
            "engine": {"S": ENGINE},
            "created_at": {"S": now},
            "updated_at": {"S": now},
            "ttl": {"N": ttl},
        }
        if alarm_data:
            item["alarm_data"] = {"S": json.dumps(alarm_data)}
        try:
            self._ddb.put_item(
                TableName=DYNAMODB_TABLE_NAME,
                Item=item,
                ConditionExpression="attribute_not_exists(SK)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def update_state(self, rca_id: str, state: str) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        self._validate_transition(rca_id, state)
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
                UpdateExpression="SET #state = :state, updated_at = :now",
                ConditionExpression=_TERMINAL_COND,
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": {"S": state},
                    ":now": {"S": _now_iso()},
                    ":completed": {"S": "COMPLETED"},
                    ":failed": {"S": "FAILED"},
                    ":outdated": {"S": "OUTDATED"},
                    ":cancelled": {"S": "CANCELLED"},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SessionCancelledError(rca_id) from e
            raise

    def mark_completed(self, rca_id: str, root_cause: str) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        self._validate_transition(rca_id, "COMPLETED")
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
                UpdateExpression="SET #state = :state, root_cause = :rc, updated_at = :now",
                ConditionExpression=_TERMINAL_COND,
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": {"S": "COMPLETED"},
                    ":rc": {"S": root_cause},
                    ":now": {"S": _now_iso()},
                    ":completed": {"S": "COMPLETED"},
                    ":failed": {"S": "FAILED"},
                    ":outdated": {"S": "OUTDATED"},
                    ":cancelled": {"S": "CANCELLED"},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.warning("mark_completed_skipped_terminal", rca_id=rca_id)
                return
            raise

    def mark_failed(self, rca_id: str, error_reason: str) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        self._validate_transition(rca_id, "FAILED")
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
                UpdateExpression="SET #state = :state, error_reason = :err, updated_at = :now",
                ConditionExpression=_TERMINAL_COND,
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": {"S": "FAILED"},
                    ":err": {"S": error_reason},
                    ":now": {"S": _now_iso()},
                    ":completed": {"S": "COMPLETED"},
                    ":failed": {"S": "FAILED"},
                    ":outdated": {"S": "OUTDATED"},
                    ":cancelled": {"S": "CANCELLED"},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.warning("mark_failed_skipped_terminal", rca_id=rca_id)
                return
            raise

    def mark_outdated(self, rca_id: str, reason: str) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
                UpdateExpression="SET #state = :state, outdated_reason = :reason, updated_at = :now",
                ConditionExpression=_TERMINAL_COND,
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": {"S": "OUTDATED"},
                    ":reason": {"S": reason},
                    ":now": {"S": _now_iso()},
                    ":completed": {"S": "COMPLETED"},
                    ":failed": {"S": "FAILED"},
                    ":outdated": {"S": "OUTDATED"},
                    ":cancelled": {"S": "CANCELLED"},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.warning("mark_outdated_skipped_terminal", rca_id=rca_id)
                return
            raise

    def is_terminated(self, rca_id: str) -> bool:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return False
        try:
            resp = self._ddb.get_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
                ProjectionExpression="#st",
                ExpressionAttributeNames={"#st": "state"},
            )
            state = resp.get("Item", {}).get("state", {}).get("S", "")
            return state in _TERMINAL_STATES
        except Exception:
            logger.exception("termination_check_failed", rca_id=rca_id)
            return False
