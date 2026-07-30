from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import structlog
from botocore.exceptions import ClientError

from cc_headless.config.settings import DYNAMODB_TABLE_NAME, SESSION_TTL_DAYS
from cc_headless.ports.interfaces.execution_store import (
    ExecutionClaim,
    ExecutionClaimDisposition,
    ExecutionClaimLostError,
    ExecutionStorePort,
    ExecutionTarget,
    ExecutionTargetUnavailableError,
)
from cc_headless.services.execution_state import (
    ExecutionState,
    assert_transition,
    is_terminal,
    parse_state,
)

logger = structlog.get_logger()

# 실행 항목의 SK 에는 엔진명을 넣지 않는다. 실행 경로는 엔진과 무관하게 하나이므로
# 엔진으로 분리할 대상이 아니며, 어느 엔진의 리포트를 실행했는지는 항목이 보유한다.
_EXEC_SK = "EXEC#{execution_id}"
_PLAYBOOK_REVISION_SK = "{engine}#PLAYBOOK_REVISION"

_CLAIM_OWNED = "attribute_exists(SK) AND claim_token = :claim"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ttl() -> str:
    return str(int(time.time()) + SESSION_TTL_DAYS * 86400)


def _summary_attribute(summary: dict) -> dict:
    rendered: dict = {}
    for key, value in summary.items():
        if isinstance(value, bool):
            rendered[key] = {"BOOL": value}
        elif isinstance(value, (int, float)):
            rendered[key] = {"N": str(value)}
        elif value is None:
            rendered[key] = {"NULL": True}
        else:
            rendered[key] = {"S": str(value)[:500]}
    return {"M": rendered}


class DynamoDbExecutionStore(ExecutionStorePort):
    def __init__(self, dynamodb_client=None):
        self._ddb = dynamodb_client

    def _key(self, rca_id: str, execution_id: str) -> dict:
        return {
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": _EXEC_SK.format(execution_id=execution_id)},
        }

    def _get_execution(self, rca_id: str, execution_id: str) -> dict | None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return None
        resp = self._ddb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key=self._key(rca_id, execution_id),
            ConsistentRead=True,
        )
        return resp.get("Item")

    def claim_execution(
        self,
        execution_id: str,
        *,
        rca_id: str,
        engine: str,
        approval_id: str,
        requested_by: str,
        claim_seconds: int,
    ) -> ExecutionClaim:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return ExecutionClaim(ExecutionClaimDisposition.CONTENDED)

        claim_token = uuid.uuid4().hex
        now = _now_iso()
        now_epoch = int(time.time())
        expires_at = now_epoch + max(claim_seconds, 60)
        item = {
            **self._key(rca_id, execution_id),
            "execution_id": {"S": execution_id},
            "rca_id": {"S": rca_id},
            "engine": {"S": engine},
            "approval_id": {"S": approval_id},
            "requested_by": {"S": requested_by or "unknown"},
            "execution_state": {"S": str(ExecutionState.EXECUTING)},
            "claim_token": {"S": claim_token},
            "claim_expires_at": {"N": str(expires_at)},
            "attempt": {"N": "1"},
            "created_at": {"S": now},
            "updated_at": {"S": now},
            "ttl": {"N": _ttl()},
        }

        try:
            self._ddb.put_item(
                TableName=DYNAMODB_TABLE_NAME,
                Item=item,
                ConditionExpression="attribute_not_exists(SK)",
            )
            return ExecutionClaim(ExecutionClaimDisposition.CLAIMED, claim_token, 1)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

        existing = self._get_execution(rca_id, execution_id)
        if not existing:
            return ExecutionClaim(ExecutionClaimDisposition.CONTENDED)

        state = parse_state(existing.get("execution_state", {}).get("S"))
        if state is not None and is_terminal(state):
            # 이미 끝난 실행의 재전달이다. 다시 실행하면 승인 한 번이 두 번의 쓰기가
            # 된다.
            return ExecutionClaim(ExecutionClaimDisposition.TERMINAL_DUPLICATE)

        previous_expires = existing.get("claim_expires_at", {}).get("N")
        if previous_expires is not None and int(previous_expires) > now_epoch:
            # 다른 워커의 claim 이 아직 유효하다. 실행 중일 수 있으므로 집지 않는다.
            return ExecutionClaim(ExecutionClaimDisposition.CONTENDED)

        attempt = int(existing.get("attempt", {}).get("N", "1")) + 1
        item["attempt"] = {"N": str(attempt)}
        item["created_at"] = existing.get("created_at", {"S": now})
        condition = _CLAIM_OWNED.replace("claim_token = :claim", "attribute_exists(SK)")
        values: dict = {}
        previous_claim = existing.get("claim_token", {}).get("S")
        if previous_claim:
            condition += " AND claim_token = :previous_claim"
            values[":previous_claim"] = {"S": previous_claim}
        if previous_expires is None:
            condition += " AND attribute_not_exists(claim_expires_at)"
        else:
            condition += " AND claim_expires_at = :previous_expires"
            values[":previous_expires"] = {"N": previous_expires}

        try:
            self._ddb.put_item(
                TableName=DYNAMODB_TABLE_NAME,
                Item=item,
                ConditionExpression=condition,
                **({"ExpressionAttributeValues": values} if values else {}),
            )
            return ExecutionClaim(ExecutionClaimDisposition.CLAIMED, claim_token, attempt)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return ExecutionClaim(ExecutionClaimDisposition.CONTENDED)
            raise

    def load_target(self, rca_id: str, engine: str) -> ExecutionTarget:
        """승인된 리포트의 플레이북을 로드한다.

        회고가 갱신한 개정본이 있으면 그것을 쓴다. 개정본이 곧 다음 실행의 근거라는
        것이 회고 루프의 목적이다.
        """
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            raise ExecutionTargetUnavailableError(f"{rca_id}: execution store is unavailable")

        resp = self._ddb.query(
            TableName=DYNAMODB_TABLE_NAME,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": f"RCA#{rca_id}"}},
            ConsistentRead=True,
        )
        items = resp.get("Items", [])

        session = next(
            (item for item in items if item.get("SK", {}).get("S") == f"{engine}#SESSION"),
            None,
        )
        if session is None:
            raise ExecutionTargetUnavailableError(f"{rca_id}: {engine} analysis session is missing")
        session_state = session.get("state", {}).get("S", "")
        if session_state != "COMPLETED":
            # 완료되지 않은 분석의 플레이북은 승인 대상이 아니다.
            raise ExecutionTargetUnavailableError(
                f"{rca_id}: {engine} analysis is {session_state or 'unknown'}, not COMPLETED"
            )

        revision_sk = _PLAYBOOK_REVISION_SK.format(engine=engine)
        revision = next((item for item in items if item.get("SK", {}).get("S") == revision_sk), None)
        playbook = _decode_playbook(revision) if revision else None
        if playbook is None:
            playbook = _playbook_from_span(items, engine)
        if playbook is None:
            raise ExecutionTargetUnavailableError(f"{rca_id}: {engine} playbook is not available")

        return ExecutionTarget(
            rca_id=rca_id,
            engine=engine,
            alarm_name=session.get("alarm_name", {}).get("S", ""),
            playbook=playbook,
            alarm_data=_decode_json_object(session.get("alarm_data", {}).get("S")),
            report_s3_key=session.get("report_s3_key", {}).get("S", ""),
        )

    def update_state(
        self,
        execution_id: str,
        *,
        rca_id: str,
        state: ExecutionState,
        claim_token: str,
        summary: dict | None = None,
        error_reason: str = "",
        evidence_s3_key: str = "",
    ) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return

        current_item = self._get_execution(rca_id, execution_id)
        if current_item is None:
            raise ExecutionClaimLostError(f"{execution_id}: execution item is missing")
        if current_item.get("claim_token", {}).get("S") != claim_token:
            raise ExecutionClaimLostError(f"{execution_id}: claim is no longer held")
        current = parse_state(current_item.get("execution_state", {}).get("S"))
        if current is None:
            raise ExecutionClaimLostError(f"{execution_id}: execution state is unreadable")
        assert_transition(current, state)

        sets = ["execution_state = :state", "updated_at = :now"]
        values: dict = {
            ":state": {"S": str(state)},
            ":now": {"S": _now_iso()},
            ":claim": {"S": claim_token},
        }
        if summary is not None:
            sets.append("evidence_summary = :summary")
            values[":summary"] = _summary_attribute(summary)
        if error_reason:
            sets.append("error_reason = :error")
            values[":error"] = {"S": error_reason[:1000]}
        if evidence_s3_key:
            sets.append("evidence_s3_key = :evidence_key")
            values[":evidence_key"] = {"S": evidence_s3_key}

        update = "SET " + ", ".join(sets)
        if is_terminal(state):
            # 종료된 실행은 claim 을 놓아 재전달이 실행 중으로 오인되지 않게 한다.
            update += " REMOVE claim_expires_at"

        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=self._key(rca_id, execution_id),
                UpdateExpression=update,
                ConditionExpression=_CLAIM_OWNED,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ExecutionClaimLostError(f"{execution_id}: claim is no longer held") from exc
            raise

    def load_state(self, execution_id: str, *, rca_id: str) -> ExecutionState | None:
        item = self._get_execution(rca_id, execution_id)
        if item is None:
            return None
        return parse_state(item.get("execution_state", {}).get("S"))

    def claim_retrospective(self, execution_id: str, *, rca_id: str, claim_token: str) -> bool:
        """실행 단위로 회고를 한 번만 수행하도록 보장한다."""
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return False
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=self._key(rca_id, execution_id),
                UpdateExpression="SET retrospective_status = :running, updated_at = :now",
                ConditionExpression=(
                    f"{_CLAIM_OWNED} AND execution_state = :resolved AND attribute_not_exists(retrospective_status)"
                ),
                ExpressionAttributeValues={
                    ":claim": {"S": claim_token},
                    ":running": {"S": "RUNNING"},
                    ":resolved": {"S": str(ExecutionState.RESOLVED)},
                    ":now": {"S": _now_iso()},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def record_retrospective(
        self,
        execution_id: str,
        *,
        rca_id: str,
        claim_token: str,
        status: str,
        summary: str,
        playbook_snapshot_s3_key: str = "",
        diff_s3_key: str = "",
    ) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        sets = [
            "retrospective_status = :status",
            "retrospective_summary = :summary",
            "updated_at = :now",
        ]
        values: dict = {
            ":claim": {"S": claim_token},
            ":status": {"S": status},
            ":summary": {"S": summary[:1000]},
            ":now": {"S": _now_iso()},
        }
        if playbook_snapshot_s3_key:
            sets.append("playbook_snapshot_s3_key = :snapshot")
            values[":snapshot"] = {"S": playbook_snapshot_s3_key}
        if diff_s3_key:
            sets.append("retrospective_diff_s3_key = :diff")
            values[":diff"] = {"S": diff_s3_key}

        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=self._key(rca_id, execution_id),
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=_CLAIM_OWNED,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ExecutionClaimLostError(f"{execution_id}: claim is no longer held") from exc
            raise

    def save_playbook_revision(
        self,
        rca_id: str,
        engine: str,
        playbook: dict,
        *,
        execution_id: str,
    ) -> None:
        """갱신된 플레이북을 같은 식별자로 저장한다.

        새 식별자로 분기하면 같은 장애 유형의 지식이 흩어지므로, 개정본은 항목을
        덮어쓰며 어느 실행이 갱신했는지만 함께 남긴다.
        """
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        now = _now_iso()
        self._ddb.put_item(
            TableName=DYNAMODB_TABLE_NAME,
            Item={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": _PLAYBOOK_REVISION_SK.format(engine=engine)},
                "engine": {"S": engine},
                "playbook_id": {"S": str(playbook.get("playbook_id", ""))[:200]},
                "playbook": {"S": json.dumps(playbook, ensure_ascii=False)},
                "revised_by_execution_id": {"S": execution_id},
                "updated_at": {"S": now},
                "ttl": {"N": _ttl()},
            },
        )


def _decode_json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _decode_playbook(item: dict) -> dict | None:
    raw = item.get("playbook", {}).get("S")
    if not raw:
        return None
    parsed = _decode_json_object(raw)
    if not parsed:
        logger.warning("playbook_revision_malformed")
        return None
    return parsed


_PLAYBOOK_STRING_FIELDS = (
    "playbook_id",
    "failure_type",
    "symptom_pattern",
    "severity_criteria",
    "temporary_mitigation",
    "permanent_remediation",
    "escalation_criteria",
    "verification_status",
)
_PLAYBOOK_LIST_FIELDS = ("verification_steps", "prevention_measures", "related_metrics", "tags")
_EXECUTION_STEP_FIELDS = ("step_id", "intent", "action", "success_criteria")


def _playbook_from_span(items: list[dict], engine: str) -> dict | None:
    """분석이 남긴 PLAYBOOK 스팬의 메타데이터를 플레이북으로 복원한다.

    회고 개정본이 아직 없는 첫 실행에서 쓰는 경로다.
    """
    span = next(
        (
            item
            for item in items
            if item.get("SK", {}).get("S", "").startswith(f"{engine}#SPAN#")
            and item.get("span_type", {}).get("S") == "PLAYBOOK"
        ),
        None,
    )
    if span is None:
        return None
    metadata = span.get("metadata", {}).get("M")
    if not isinstance(metadata, dict):
        return None

    playbook: dict = {"stage": "PLAYBOOK"}
    for field in _PLAYBOOK_STRING_FIELDS:
        value = metadata.get(field, {}).get("S")
        if value:
            playbook[field] = value
    for field in _PLAYBOOK_LIST_FIELDS:
        entries = metadata.get(field, {}).get("L")
        if isinstance(entries, list):
            playbook[field] = [entry.get("S", "") for entry in entries if isinstance(entry, dict)]

    steps: list[dict] = []
    for entry in metadata.get("execution_steps", {}).get("L") or []:
        if not isinstance(entry, dict):
            continue
        rendered = entry.get("M")
        if not isinstance(rendered, dict):
            continue
        step = {field: rendered.get(field, {}).get("S", "") for field in _EXECUTION_STEP_FIELDS}
        if step["step_id"]:
            steps.append(step)
    playbook["execution_steps"] = steps
    return playbook
