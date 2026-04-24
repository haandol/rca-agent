from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import boto3
import structlog
from botocore.exceptions import ClientError

from cc_headless.config import (
    DYNAMODB_TABLE_NAME,
    ENGINE,
    SESSION_TTL_DAYS,
)

logger = structlog.get_logger()

_ddb = boto3.client("dynamodb")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ttl() -> str:
    return str(int(time.time()) + SESSION_TTL_DAYS * 86400)


def build_rca_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))


def check_duplicate(rca_id: str) -> bool:
    if not DYNAMODB_TABLE_NAME:
        return False
    resp = _ddb.get_item(
        TableName=DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
    )
    return "Item" in resp


def create_session(
    rca_id: str,
    alarm_name: str,
    idempotency_key: str,
    *,
    alarm_data: dict | None = None,
) -> bool:
    if not DYNAMODB_TABLE_NAME:
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
        _ddb.put_item(
            TableName=DYNAMODB_TABLE_NAME,
            Item=item,
            ConditionExpression="attribute_not_exists(SK)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def update_state(rca_id: str, state: str) -> None:
    if not DYNAMODB_TABLE_NAME:
        return
    _ddb.update_item(
        TableName=DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
        UpdateExpression="SET #state = :state, updated_at = :now",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":state": {"S": state}, ":now": {"S": _now_iso()}},
    )


def mark_completed(rca_id: str, root_cause: str) -> None:
    if not DYNAMODB_TABLE_NAME:
        return
    _ddb.update_item(
        TableName=DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
        UpdateExpression="SET #state = :state, root_cause = :rc, updated_at = :now",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={
            ":state": {"S": "COMPLETED"},
            ":rc": {"S": root_cause},
            ":now": {"S": _now_iso()},
        },
    )


def mark_failed(rca_id: str, error_reason: str) -> None:
    if not DYNAMODB_TABLE_NAME:
        return
    _ddb.update_item(
        TableName=DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": f"{ENGINE}#SESSION"}},
        UpdateExpression="SET #state = :state, error_reason = :err, updated_at = :now",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={
            ":state": {"S": "FAILED"},
            ":err": {"S": error_reason},
            ":now": {"S": _now_iso()},
        },
    )
