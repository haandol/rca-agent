from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from rca_agent.config import DYNAMODB_TABLE_NAME, SESSION_TTL_DAYS
from rca_agent.models import AlarmPayload, RcaSession, RcaSessionState

logger = logging.getLogger(__name__)


def build_idempotency_key(alarm: AlarmPayload) -> str:
    ts = alarm.state_change_time.isoformat() if alarm.state_change_time else "unknown"
    return f"{alarm.alarm_name}#{ts}"


def create_session(
    alarm: AlarmPayload,
    *,
    dynamodb_client=None,
) -> RcaSession | None:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return None

    rca_id = str(uuid.uuid4())
    idempotency_key = build_idempotency_key(alarm)
    now = datetime.now(UTC)
    ttl = int(time.time()) + SESSION_TTL_DAYS * 86400

    item = {
        "PK": {"S": f"RCA#{rca_id}"},
        "SK": {"S": "SESSION"},
        "rca_id": {"S": rca_id},
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
            ConditionExpression="attribute_not_exists(idempotency_key) OR idempotency_key <> :ik",
            ExpressionAttributeValues={":ik": {"S": idempotency_key}},
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

    try:
        response = dynamodb_client.query(
            TableName=DYNAMODB_TABLE_NAME,
            IndexName="idempotency-index",
            KeyConditionExpression="idempotency_key = :ik",
            ExpressionAttributeValues={":ik": {"S": idempotency_key}},
            Limit=1,
        )
        if response.get("Items"):
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

    now = datetime.now(UTC).isoformat()

    try:
        dynamodb_client.update_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": "SESSION"},
            },
            UpdateExpression="SET #st = :state, updated_at = :now",
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues={
                ":state": {"S": new_state.value},
                ":now": {"S": now},
            },
        )
        logger.info("Session %s state updated to %s", rca_id, new_state.value)
        return True
    except ClientError:
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

    now = datetime.now(UTC).isoformat()

    try:
        dynamodb_client.update_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": "SESSION"},
            },
            UpdateExpression="SET #st = :state, updated_at = :now, root_cause = :rc, confirmed = :cf",
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


def mark_failed(
    rca_id: str,
    *,
    error_reason: str = "",
    dynamodb_client=None,
) -> bool:
    if not DYNAMODB_TABLE_NAME or dynamodb_client is None:
        return False

    now = datetime.now(UTC).isoformat()

    try:
        dynamodb_client.update_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": "SESSION"},
            },
            UpdateExpression="SET #st = :state, updated_at = :now, error_reason = :err",
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
