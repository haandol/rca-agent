from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import structlog
from botocore.exceptions import ClientError

from cc_headless.config.settings import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS
from cc_headless.ports.interfaces.session_store import (
    ClaimDisposition,
    SessionCancelledError,
    SessionClaim,
    SessionOwnershipCheckError,
    SessionStorePort,
    SideEffectLeaseUnavailableError,
)

logger = structlog.get_logger()

_TERMINAL_COND = (
    "attribute_exists(SK) AND claim_token = :claim AND NOT #state IN (:completed, :failed, :outdated, :cancelled)"
)


def build_rca_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))


class InvalidStateTransitionError(Exception):
    pass


VALID_TRANSITIONS: dict[str, set[str]] = {
    "ALARM_RECEIVED": {"ANALYZING", "FAILED", "OUTDATED", "CANCELLED"},
    "ANALYZING": {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"},
}

_TERMINAL_STATES = {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"}
_DEDUPE_STATES = {"COMPLETED", "OUTDATED", "CANCELLED"}
_RECLAIMABLE_STATES = {"ALARM_RECEIVED", "ANALYZING", "FAILED"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ttl() -> str:
    return str(int(time.time()) + SESSION_TTL_DAYS * 86400)


class DynamoDbSessionStore(SessionStorePort):
    def __init__(self, dynamodb_client=None):
        self._ddb = dynamodb_client

    def _get_session(self, rca_id: str) -> dict | None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return None
        resp = self._ddb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
            ConsistentRead=True,
        )
        return resp.get("Item")

    def _validate_transition(self, rca_id: str, target: str, claim_token: str) -> None:
        item = self._get_session(rca_id)
        if item is None:
            raise SessionOwnershipCheckError(f"{rca_id}: session item is missing")
        if item.get("claim_token", {}).get("S") != claim_token:
            raise SessionCancelledError(rca_id)
        current = item["state"]["S"]
        if current in _TERMINAL_STATES:
            raise SessionCancelledError(rca_id)
        allowed = VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidStateTransitionError(f"{rca_id}: {current} → {target}")

    def claim_session(
        self,
        rca_id: str,
        alarm_name: str,
        idempotency_key: str,
        *,
        receive_count: int,
        alarm_data: dict | None = None,
    ) -> SessionClaim:
        receive_count = max(receive_count, 1)
        claim_token = uuid.uuid4().hex
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return SessionClaim(ClaimDisposition.CONTENDED)
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
            "claim_token": {"S": claim_token},
            "receive_count": {"N": str(receive_count)},
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
            return SessionClaim(ClaimDisposition.CLAIMED, claim_token, receive_count)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

        existing = self._get_session(rca_id)
        if not existing:
            return SessionClaim(ClaimDisposition.CONTENDED)
        state = existing.get("state", {}).get("S", "")
        if state in _DEDUPE_STATES:
            return SessionClaim(ClaimDisposition.TERMINAL_DUPLICATE)
        if state not in _RECLAIMABLE_STATES:
            return SessionClaim(ClaimDisposition.CONTENDED)

        previous_receive_count_attr = existing.get("receive_count")
        previous_receive_count = int(previous_receive_count_attr.get("N", "0")) if previous_receive_count_attr else 0
        if receive_count <= previous_receive_count:
            return SessionClaim(ClaimDisposition.CONTENDED)

        previous_claim = existing.get("claim_token", {}).get("S")
        condition = (
            "#state = :previous_state AND "
            "(attribute_not_exists(side_effect_lease_expires_at) OR side_effect_lease_expires_at < :now_epoch)"
        )
        expression_names = {"#state": "state"}
        expression_values = {
            ":previous_state": {"S": state},
            ":now_epoch": {"N": str(int(time.time()))},
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

        try:
            self._ddb.put_item(
                TableName=DYNAMODB_TABLE_NAME,
                Item=item,
                ConditionExpression=condition,
                ExpressionAttributeNames=expression_names,
                ExpressionAttributeValues=expression_values,
            )
            return SessionClaim(ClaimDisposition.CLAIMED, claim_token, receive_count)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return SessionClaim(ClaimDisposition.CONTENDED)
            raise

    def update_state(self, rca_id: str, state: str, *, claim_token: str) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        self._validate_transition(rca_id, state, claim_token)
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
                    ":claim": {"S": claim_token},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SessionCancelledError(rca_id) from e
            raise

    def mark_completed(
        self,
        rca_id: str,
        root_cause: str,
        report_s3_key: str,
        *,
        claim_token: str,
        side_effect_lease_token: str | None = None,
    ) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        self._validate_transition(rca_id, "COMPLETED", claim_token)
        condition = _TERMINAL_COND
        values = {
            ":state": {"S": "COMPLETED"},
            ":rc": {"S": root_cause},
            ":report_s3_key": {"S": report_s3_key},
            ":now": {"S": _now_iso()},
            ":completed": {"S": "COMPLETED"},
            ":failed": {"S": "FAILED"},
            ":outdated": {"S": "OUTDATED"},
            ":cancelled": {"S": "CANCELLED"},
            ":claim": {"S": claim_token},
        }
        update = (
            "SET #state = :state, root_cause = :rc, report_s3_key = :report_s3_key, updated_at = :now "
            "REMOVE side_effect_lease_token, side_effect_lease_name, side_effect_lease_expires_at"
        )
        if side_effect_lease_token:
            condition += " AND side_effect_lease_token = :lease"
            values[":lease"] = {"S": side_effect_lease_token}
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=values,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SessionCancelledError(rca_id) from e
            raise

    def mark_failed(self, rca_id: str, error_reason: str, *, claim_token: str) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        self._validate_transition(rca_id, "FAILED", claim_token)
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
                    ":claim": {"S": claim_token},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SessionCancelledError(rca_id) from e
            raise

    def mark_outdated(self, rca_id: str, reason: str, *, claim_token: str) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        self._validate_transition(rca_id, "OUTDATED", claim_token)
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
                    ":claim": {"S": claim_token},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SessionCancelledError(rca_id) from e
            raise

    def is_terminated(self, rca_id: str, *, claim_token: str) -> bool:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return False
        try:
            item = self._get_session(rca_id)
            if item is None:
                return True
            state = item.get("state", {}).get("S", "")
            owner = item.get("claim_token", {}).get("S")
            return state in _TERMINAL_STATES or owner != claim_token
        except Exception as exc:
            logger.exception("termination_check_failed", rca_id=rca_id)
            raise SessionOwnershipCheckError(rca_id) from exc

    def acquire_side_effect_lease(
        self,
        rca_id: str,
        *,
        claim_token: str,
        effect_name: str,
        lease_seconds: int,
    ) -> str:
        lease_token = uuid.uuid4().hex
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            raise SideEffectLeaseUnavailableError(f"{rca_id}: DynamoDB session store is unavailable")
        now_epoch = int(time.time())
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
                UpdateExpression=(
                    "SET side_effect_lease_token = :lease, side_effect_lease_name = :effect, "
                    "side_effect_lease_expires_at = :expires, updated_at = :now"
                ),
                ConditionExpression=(
                    "attribute_exists(SK) AND claim_token = :claim AND #state = :analyzing AND "
                    "(attribute_not_exists(side_effect_lease_expires_at) "
                    "OR side_effect_lease_expires_at < :now_epoch)"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":claim": {"S": claim_token},
                    ":analyzing": {"S": "ANALYZING"},
                    ":lease": {"S": lease_token},
                    ":effect": {"S": effect_name},
                    ":expires": {"N": str(now_epoch + max(lease_seconds, 1))},
                    ":now_epoch": {"N": str(now_epoch)},
                    ":now": {"S": _now_iso()},
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SideEffectLeaseUnavailableError(f"{rca_id}: {effect_name}") from exc
            raise
        return lease_token

    def release_side_effect_lease(
        self,
        rca_id: str,
        *,
        claim_token: str,
        lease_token: str,
    ) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
                UpdateExpression=(
                    "REMOVE side_effect_lease_token, side_effect_lease_name, side_effect_lease_expires_at "
                    "SET updated_at = :now"
                ),
                ConditionExpression=(
                    "attribute_exists(SK) AND claim_token = :claim AND side_effect_lease_token = :lease"
                ),
                ExpressionAttributeValues={
                    ":claim": {"S": claim_token},
                    ":lease": {"S": lease_token},
                    ":now": {"S": _now_iso()},
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SessionCancelledError(rca_id) from exc
            raise
