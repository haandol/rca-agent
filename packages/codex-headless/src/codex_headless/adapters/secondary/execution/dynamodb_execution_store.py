from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from codex_headless.config.settings import DYNAMODB_TABLE_NAME, SESSION_TTL_DAYS
from codex_headless.ports.interfaces.execution_store import (
    ExecutionClaim,
    ExecutionClaimDisposition,
    ExecutionClaimLostError,
    ExecutionStorePort,
    ExecutionTarget,
    ExecutionTargetUnavailableError,
)
from codex_headless.services.execution_state import (
    ExecutionState,
    assert_transition,
    is_terminal,
    parse_state,
)

# 실행 항목의 SK 에는 엔진명을 넣지 않는다. 실행 경로는 엔진과 무관하게 하나이므로
# 엔진으로 분리할 대상이 아니며, 어느 엔진의 리포트를 실행했는지는 항목이 보유한다.
_EXEC_SK = "EXEC#{execution_id}"
_ACTIVE_EXEC_SK = "EXEC_ACTIVE"
_PLAYBOOK_REVISION_SK = "{engine}#PLAYBOOK_REVISION"
_PLAYBOOK_REVISION_STAGE_SK = "{engine}#PLAYBOOK_REVISION_STAGE#{execution_id}"

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

    def _active_key(self, rca_id: str) -> dict:
        return {
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": _ACTIVE_EXEC_SK},
        }

    def _get_active_execution(self, rca_id: str) -> dict | None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return None
        response = self._ddb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key=self._active_key(rca_id),
            ConsistentRead=True,
        )
        return response.get("Item")

    def claim_execution(
        self,
        execution_id: str,
        *,
        rca_id: str,
        engine: str,
        approval_id: str,
        requested_by: str,
        report_s3_key: str,
        approved_playbook_s3_key: str,
        playbook_digest: str,
        claim_seconds: int,
    ) -> ExecutionClaim:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return ExecutionClaim(ExecutionClaimDisposition.CONTENDED)

        existing = self._get_execution(rca_id, execution_id)
        if not existing:
            return ExecutionClaim(ExecutionClaimDisposition.REJECTED)

        state = parse_state(existing.get("execution_state", {}).get("S"))
        if state is None:
            return ExecutionClaim(ExecutionClaimDisposition.REJECTED)
        if is_terminal(state):
            return ExecutionClaim(ExecutionClaimDisposition.TERMINAL_DUPLICATE)

        expected = {
            "execution_id": execution_id,
            "rca_id": rca_id,
            "engine": engine,
            "approval_id": approval_id,
            "requested_by": requested_by,
            "report_s3_key": report_s3_key,
            "approved_playbook_s3_key": approved_playbook_s3_key,
            "playbook_digest": playbook_digest,
        }
        if any(existing.get(field, {}).get("S") != value for field, value in expected.items()):
            return ExecutionClaim(ExecutionClaimDisposition.REJECTED)

        active = self._get_active_execution(rca_id)
        if active is None or active.get("execution_id", {}).get("S") != execution_id:
            return ExecutionClaim(ExecutionClaimDisposition.REJECTED)

        claim_token = uuid.uuid4().hex
        now = _now_iso()
        now_epoch = int(time.time())
        expires_at = now_epoch + max(claim_seconds, 60)
        previous_expires = existing.get("claim_expires_at", {}).get("N")
        if state is not ExecutionState.PENDING_APPROVAL:
            if previous_expires is None:
                return ExecutionClaim(ExecutionClaimDisposition.REJECTED)
            if int(previous_expires) > now_epoch:
                return ExecutionClaim(ExecutionClaimDisposition.CONTENDED)
            if self._fail_expired_execution(
                execution_id,
                rca_id=rca_id,
                state=state,
                claim_token=existing.get("claim_token", {}).get("S", ""),
                claim_expires_at=previous_expires,
            ):
                return ExecutionClaim(ExecutionClaimDisposition.EXPIRED_FAILED)
            return ExecutionClaim(ExecutionClaimDisposition.CONTENDED)

        try:
            self._ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": DYNAMODB_TABLE_NAME,
                            "Key": self._key(rca_id, execution_id),
                            "UpdateExpression": (
                                "SET #state = :executing, claim_token = :claim, "
                                "claim_expires_at = :expires, attempt = :attempt, "
                                "started_at = :now, updated_at = :now"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(SK) AND #state = :pending "
                                "AND execution_id = :execution_id AND rca_id = :rca_id "
                                "AND engine = :engine AND approval_id = :approval_id "
                                "AND requested_by = :requested_by AND report_s3_key = :report_s3_key "
                                "AND approved_playbook_s3_key = :snapshot_key "
                                "AND playbook_digest = :digest"
                            ),
                            "ExpressionAttributeNames": {"#state": "execution_state"},
                            "ExpressionAttributeValues": {
                                ":pending": {"S": str(ExecutionState.PENDING_APPROVAL)},
                                ":executing": {"S": str(ExecutionState.EXECUTING)},
                                ":execution_id": {"S": execution_id},
                                ":rca_id": {"S": rca_id},
                                ":engine": {"S": engine},
                                ":approval_id": {"S": approval_id},
                                ":requested_by": {"S": requested_by},
                                ":report_s3_key": {"S": report_s3_key},
                                ":snapshot_key": {"S": approved_playbook_s3_key},
                                ":digest": {"S": playbook_digest},
                                ":claim": {"S": claim_token},
                                ":expires": {"N": str(expires_at)},
                                ":attempt": {"N": "1"},
                                ":now": {"S": now},
                            },
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": DYNAMODB_TABLE_NAME,
                            "Key": self._active_key(rca_id),
                            "ConditionExpression": "attribute_exists(SK) AND execution_id = :execution_id",
                            "ExpressionAttributeValues": {
                                ":execution_id": {"S": execution_id},
                            },
                        }
                    },
                ]
            )
            return ExecutionClaim(ExecutionClaimDisposition.CLAIMED, claim_token, 1)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                return ExecutionClaim(ExecutionClaimDisposition.CONTENDED)
            raise

    def _fail_expired_execution(
        self,
        execution_id: str,
        *,
        rca_id: str,
        state: ExecutionState | None,
        claim_token: str,
        claim_expires_at: str,
    ) -> bool:
        if state is None or not claim_token:
            return False
        reason = "execution claim expired; user reapproval is required before remediation can run again"
        try:
            self._ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": DYNAMODB_TABLE_NAME,
                            "Key": self._key(rca_id, execution_id),
                            "UpdateExpression": (
                                "SET execution_state = :failed, error_reason = :reason, updated_at = :now "
                                "REMOVE claim_expires_at"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(SK) AND execution_state = :current "
                                "AND claim_token = :claim AND claim_expires_at = :expires"
                            ),
                            "ExpressionAttributeValues": {
                                ":failed": {"S": str(ExecutionState.FAILED)},
                                ":reason": {"S": reason},
                                ":now": {"S": _now_iso()},
                                ":current": {"S": str(state)},
                                ":claim": {"S": claim_token},
                                ":expires": {"N": claim_expires_at},
                            },
                        }
                    },
                    {
                        "Delete": {
                            "TableName": DYNAMODB_TABLE_NAME,
                            "Key": self._active_key(rca_id),
                            "ConditionExpression": "attribute_exists(SK) AND execution_id = :execution_id",
                            "ExpressionAttributeValues": {
                                ":execution_id": {"S": execution_id},
                            },
                        }
                    },
                ]
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                return False
            raise

    def load_target(
        self,
        rca_id: str,
        engine: str,
        *,
        report_s3_key: str,
        playbook: dict,
    ) -> ExecutionTarget:
        """완료 세션의 알람 컨텍스트를 승인 스냅샷과 결합한다."""
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            raise ExecutionTargetUnavailableError(f"{rca_id}: execution store is unavailable")

        response = self._ddb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {"S": f"{engine}#SESSION"},
            },
            ConsistentRead=True,
        )
        session = response.get("Item")
        if session is None:
            raise ExecutionTargetUnavailableError(f"{rca_id}: {engine} analysis session is missing")
        session_state = session.get("state", {}).get("S", "")
        if session_state != "COMPLETED":
            # 완료되지 않은 분석의 플레이북은 승인 대상이 아니다.
            raise ExecutionTargetUnavailableError(
                f"{rca_id}: {engine} analysis is {session_state or 'unknown'}, not COMPLETED"
            )
        session_report_key = session.get("report_s3_key", {}).get("S", "")
        if session_report_key != report_s3_key:
            raise ExecutionTargetUnavailableError(
                f"{rca_id}: approved report key does not match the completed analysis session"
            )

        return ExecutionTarget(
            rca_id=rca_id,
            engine=engine,
            alarm_name=session.get("alarm_name", {}).get("S", ""),
            playbook=playbook,
            alarm_data=_decode_json_object(session.get("alarm_data", {}).get("S")),
            report_s3_key=session_report_key,
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
        retrospective_failure_reason: str = "",
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
        evidence_s3_key = evidence_s3_key.strip()
        if evidence_s3_key:
            sets.append("evidence_s3_key = :evidence_key")
            values[":evidence_key"] = {"S": evidence_s3_key}
        if retrospective_failure_reason:
            sets.extend(
                [
                    "retrospective_status = :retrospective_failed",
                    "retrospective_summary = :retrospective_summary",
                ]
            )
            values[":retrospective_failed"] = {"S": "FAILED"}
            values[":retrospective_summary"] = {"S": retrospective_failure_reason[:1000]}

        update = "SET " + ", ".join(sets)
        if is_terminal(state):
            # 종료된 실행은 claim 을 놓아 재전달이 실행 중으로 오인되지 않게 한다.
            update += " REMOVE claim_expires_at"

        try:
            if is_terminal(state):
                values[":current"] = {"S": str(current)}
                self._ddb.transact_write_items(
                    TransactItems=[
                        {
                            "Update": {
                                "TableName": DYNAMODB_TABLE_NAME,
                                "Key": self._key(rca_id, execution_id),
                                "UpdateExpression": update,
                                "ConditionExpression": f"{_CLAIM_OWNED} AND execution_state = :current",
                                "ExpressionAttributeValues": values,
                            }
                        },
                        {
                            "Delete": {
                                "TableName": DYNAMODB_TABLE_NAME,
                                "Key": self._active_key(rca_id),
                                "ConditionExpression": "attribute_exists(SK) AND execution_id = :execution_id",
                                "ExpressionAttributeValues": {
                                    ":execution_id": {"S": execution_id},
                                },
                            }
                        },
                    ]
                )
            else:
                self._ddb.update_item(
                    TableName=DYNAMODB_TABLE_NAME,
                    Key=self._key(rca_id, execution_id),
                    UpdateExpression=update,
                    ConditionExpression=_CLAIM_OWNED,
                    ExpressionAttributeValues=values,
                )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
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
                    f"{_CLAIM_OWNED} AND execution_state = :resolved "
                    "AND attribute_exists(evidence_s3_key) AND evidence_s3_key <> :empty "
                    "AND attribute_not_exists(retrospective_status)"
                ),
                ExpressionAttributeValues={
                    ":claim": {"S": claim_token},
                    ":empty": {"S": ""},
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
        """인덱스 게시 전에 실행별 개정본을 내구성 있게 준비한다."""
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        now = _now_iso()
        self._ddb.put_item(
            TableName=DYNAMODB_TABLE_NAME,
            Item={
                "PK": {"S": f"RCA#{rca_id}"},
                "SK": {
                    "S": _PLAYBOOK_REVISION_STAGE_SK.format(
                        engine=engine,
                        execution_id=execution_id,
                    )
                },
                "engine": {"S": engine},
                "playbook_id": {"S": str(playbook.get("playbook_id", ""))[:200]},
                "playbook": {"S": json.dumps(playbook, ensure_ascii=False)},
                "revised_by_execution_id": {"S": execution_id},
                "publication_status": {"S": "PENDING"},
                "updated_at": {"S": now},
                "ttl": {"N": _ttl()},
            },
        )

    def publish_playbook_revision(
        self,
        rca_id: str,
        engine: str,
        playbook: dict,
        *,
        execution_id: str,
    ) -> None:
        """준비된 개정본을 검색 인덱스와 대응하는 정식 개정본으로 확정한다."""
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        now = _now_iso()
        stage_key = {
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {
                "S": _PLAYBOOK_REVISION_STAGE_SK.format(
                    engine=engine,
                    execution_id=execution_id,
                )
            },
        }
        self._ddb.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": DYNAMODB_TABLE_NAME,
                        "Item": {
                            "PK": {"S": f"RCA#{rca_id}"},
                            "SK": {"S": _PLAYBOOK_REVISION_SK.format(engine=engine)},
                            "engine": {"S": engine},
                            "playbook_id": {"S": str(playbook.get("playbook_id", ""))[:200]},
                            "playbook": {"S": json.dumps(playbook, ensure_ascii=False)},
                            "revised_by_execution_id": {"S": execution_id},
                            "publication_status": {"S": "PUBLISHED"},
                            "updated_at": {"S": now},
                            "ttl": {"N": _ttl()},
                        },
                    }
                },
                {
                    "Delete": {
                        "TableName": DYNAMODB_TABLE_NAME,
                        "Key": stage_key,
                        "ConditionExpression": (
                            "revised_by_execution_id = :execution AND publication_status = :pending"
                        ),
                        "ExpressionAttributeValues": {
                            ":execution": {"S": execution_id},
                            ":pending": {"S": "PENDING"},
                        },
                    }
                },
            ]
        )


def _decode_json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
