from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from botocore.exceptions import ClientError

from headless_codex.config.settings import DYNAMODB_TABLE_NAME, ENGINE, SESSION_TTL_DAYS
from headless_codex.ports.interfaces.session_store import (
    ClaimDisposition,
    IncidentAlarm,
    IncidentClaim,
    IncidentClaimDisposition,
    SessionCancelledError,
    SessionClaim,
    SessionOwnershipCheckError,
    SessionStorePort,
    SideEffectLeaseUnavailableError,
)

logger = structlog.get_logger()

_ACTIVE_INCIDENT_SK = "ACTIVE_INCIDENT"
_ANALYSIS_SESSION_SK = "ANALYSIS#SESSION"
_ANALYSIS_SESSION_SKS = (
    _ANALYSIS_SESSION_SK,
    "strands#SESSION",
    "SESSION",
    "headless-codex#SESSION",
    "codex-headless#SESSION",
    "cc-headless#SESSION",
)
_ACTIVE_EXECUTION_SK = "EXEC_ACTIVE"
_INCIDENT_TERMINAL_STATES = {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"}
_INCIDENT_CLAIM_RETRIES = 3
_TERMINAL_COND = (
    "attribute_exists(SK) AND claim_token = :claim AND NOT #state IN (:completed, :failed, :outdated, :cancelled)"
)


def build_rca_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))


def build_idempotency_key(alarm: IncidentAlarm) -> str:
    timestamp = alarm.state_change_time.isoformat() if alarm.state_change_time else "unknown"
    return f"{alarm.alarm_name}#{timestamp}"


def build_alarm_identity(alarm: IncidentAlarm) -> str:
    if alarm.alarm_arn:
        return alarm.alarm_arn
    return f"cloudwatch:{alarm.region}:alarm:{alarm.alarm_name}"


def build_active_incident_pk(alarm: IncidentAlarm) -> str:
    identity_hash = hashlib.sha256(build_alarm_identity(alarm).encode()).hexdigest()
    return f"ALARM#{identity_hash}"


def _incident_key(alarm: IncidentAlarm) -> dict:
    return {
        "PK": {"S": build_active_incident_pk(alarm)},
        "SK": {"S": _ACTIVE_INCIDENT_SK},
    }


def _candidate_key(rca_id: str, sk: str) -> dict:
    return {"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": sk}}


def _session_key(rca_id: str) -> dict:
    return {"PK": {"S": f"RCA#{rca_id}"}, "SK": {"S": _ANALYSIS_SESSION_SK}}


def _event_time(alarm: IncidentAlarm) -> datetime:
    event_time = alarm.state_change_time or datetime.now(UTC)
    if event_time.tzinfo is None:
        return event_time.replace(tzinfo=UTC)
    return event_time.astimezone(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class InvalidStateTransitionError(Exception):
    pass


VALID_TRANSITIONS: dict[str, set[str]] = {
    "ALARM_RECEIVED": {"ANALYZING", "FAILED", "OUTDATED", "CANCELLED"},
    "ANALYZING": {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"},
}

_TERMINAL_STATES = {"COMPLETED", "FAILED", "OUTDATED", "CANCELLED"}
_DEDUPE_STATES = {"COMPLETED", "OUTDATED", "CANCELLED"}
_RECLAIMABLE_STATES = {
    "ALARM_RECEIVED",
    "SCOPING",
    "HYPOTHESIS_GENERATION",
    "HYPOTHESIS_PRIORITIZATION",
    "EVIDENCE_COLLECTION",
    "HYPOTHESIS_VALIDATION",
    "REPORT_GENERATION",
    "ANALYZING",
    "FAILED",
}


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
            Key=_session_key(rca_id),
            ConsistentRead=True,
        )
        return resp.get("Item")

    @property
    def _enabled(self) -> bool:
        return bool(DYNAMODB_TABLE_NAME and self._ddb)

    def _get_incident(self, alarm: IncidentAlarm) -> dict | None:
        if not self._enabled:
            return None
        response = self._ddb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key=_incident_key(alarm),
            ConsistentRead=True,
        )
        return response.get("Item")

    def _get_candidate_item(self, rca_id: str, sk: str) -> dict | None:
        response = self._ddb.get_item(
            TableName=DYNAMODB_TABLE_NAME,
            Key=_candidate_key(rca_id, sk),
            ConsistentRead=True,
        )
        return response.get("Item")

    @staticmethod
    def _incident_ttl() -> int:
        return int(time.time()) + SESSION_TTL_DAYS * 86400

    def _touch_last_alarm(
        self,
        alarm: IncidentAlarm,
        *,
        candidate_rca_id: str,
        event_time: datetime,
    ) -> None:
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_incident_key(alarm),
                UpdateExpression="SET last_alarm_at = :alarm, updated_at = :now, #ttl = :ttl",
                ConditionExpression=(
                    "candidate_rca_id = :candidate AND (attribute_not_exists(last_alarm_at) OR last_alarm_at < :alarm)"
                ),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":candidate": {"S": candidate_rca_id},
                    ":alarm": {"S": _iso(event_time)},
                    ":now": {"S": _now_iso()},
                    ":ttl": {"N": str(self._incident_ttl())},
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise SessionOwnershipCheckError(candidate_rca_id) from exc

    def _suppressed_incident(
        self,
        alarm: IncidentAlarm,
        *,
        candidate_rca_id: str,
        generation: int,
        event_time: datetime,
        reason: str,
        retryable: bool = False,
    ) -> IncidentClaim:
        self._touch_last_alarm(
            alarm,
            candidate_rca_id=candidate_rca_id,
            event_time=event_time,
        )
        return IncidentClaim(
            IncidentClaimDisposition.SUPPRESSED,
            candidate_rca_id,
            generation,
            reason,
            retryable,
        )

    def _candidate_activity(self, candidate_rca_id: str) -> tuple[bool, str]:
        for sk in _ANALYSIS_SESSION_SKS:
            item = self._get_candidate_item(candidate_rca_id, sk)
            if not item:
                continue
            state = item.get("state", {}).get("S", "")
            if state not in _INCIDENT_TERMINAL_STATES:
                return True, f"{sk} is {state or 'unknown'}"
        if self._get_candidate_item(candidate_rca_id, _ACTIVE_EXECUTION_SK):
            return True, "EXEC_ACTIVE exists"
        return False, ""

    def _open_first_incident(
        self,
        alarm: IncidentAlarm,
        *,
        candidate_rca_id: str,
        event_time: datetime,
    ) -> IncidentClaim | None:
        now = _now_iso()
        try:
            self._ddb.put_item(
                TableName=DYNAMODB_TABLE_NAME,
                Item={
                    **_incident_key(alarm),
                    "alarm_identity": {"S": build_alarm_identity(alarm)},
                    "alarm_name": {"S": alarm.alarm_name},
                    "candidate_rca_id": {"S": candidate_rca_id},
                    "generation": {"N": "1"},
                    "opened_at": {"S": _iso(event_time)},
                    "last_alarm_at": {"S": _iso(event_time)},
                    "created_at": {"S": now},
                    "updated_at": {"S": now},
                    "ttl": {"N": str(self._incident_ttl())},
                },
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise SessionOwnershipCheckError(candidate_rca_id) from exc
        return IncidentClaim(IncidentClaimDisposition.PROCEED, candidate_rca_id, 1)

    def _open_from_recovery_watermark(
        self,
        alarm: IncidentAlarm,
        *,
        recovery_at: str,
        candidate_rca_id: str,
        event_time: datetime,
    ) -> bool:
        now = _now_iso()
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_incident_key(alarm),
                UpdateExpression=(
                    "SET alarm_identity = :identity, alarm_name = :name, "
                    "candidate_rca_id = :candidate, generation = :generation, "
                    "opened_at = :opened, last_alarm_at = :alarm, updated_at = :now, #ttl = :ttl "
                    "REMOVE last_ok_at"
                ),
                ConditionExpression=(
                    "attribute_exists(SK) AND attribute_not_exists(candidate_rca_id) AND last_ok_at = :recovery"
                ),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":identity": {"S": build_alarm_identity(alarm)},
                    ":name": {"S": alarm.alarm_name},
                    ":candidate": {"S": candidate_rca_id},
                    ":generation": {"N": "1"},
                    ":opened": {"S": _iso(event_time)},
                    ":alarm": {"S": _iso(event_time)},
                    ":recovery": {"S": recovery_at},
                    ":now": {"S": now},
                    ":ttl": {"N": str(self._incident_ttl())},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise SessionOwnershipCheckError(candidate_rca_id) from exc

    def _advance_incident(
        self,
        alarm: IncidentAlarm,
        *,
        previous_candidate: str,
        previous_generation: int,
        previous_last_ok: str,
        candidate_rca_id: str,
        event_time: datetime,
    ) -> bool:
        terminal_condition = "attribute_not_exists(SK) OR #state IN (:completed, :failed, :outdated, :cancelled)"
        terminal_values = {
            ":completed": {"S": "COMPLETED"},
            ":failed": {"S": "FAILED"},
            ":outdated": {"S": "OUTDATED"},
            ":cancelled": {"S": "CANCELLED"},
        }
        condition_checks = [
            {
                "ConditionCheck": {
                    "TableName": DYNAMODB_TABLE_NAME,
                    "Key": _candidate_key(previous_candidate, sk),
                    "ConditionExpression": terminal_condition,
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": terminal_values,
                }
            }
            for sk in _ANALYSIS_SESSION_SKS
        ]
        condition_checks.append(
            {
                "ConditionCheck": {
                    "TableName": DYNAMODB_TABLE_NAME,
                    "Key": _candidate_key(previous_candidate, _ACTIVE_EXECUTION_SK),
                    "ConditionExpression": "attribute_not_exists(SK)",
                }
            }
        )
        now = _now_iso()
        update = {
            "Update": {
                "TableName": DYNAMODB_TABLE_NAME,
                "Key": _incident_key(alarm),
                "UpdateExpression": (
                    "SET candidate_rca_id = :candidate, generation = :generation, "
                    "opened_at = :opened, last_alarm_at = :alarm, updated_at = :now, #ttl = :ttl "
                    "REMOVE last_ok_at"
                ),
                "ConditionExpression": (
                    "candidate_rca_id = :previous_candidate AND generation = :previous_generation "
                    "AND last_ok_at = :previous_last_ok"
                ),
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": {
                    ":candidate": {"S": candidate_rca_id},
                    ":generation": {"N": str(previous_generation + 1)},
                    ":opened": {"S": _iso(event_time)},
                    ":alarm": {"S": _iso(event_time)},
                    ":now": {"S": now},
                    ":ttl": {"N": str(self._incident_ttl())},
                    ":previous_candidate": {"S": previous_candidate},
                    ":previous_generation": {"N": str(previous_generation)},
                    ":previous_last_ok": {"S": previous_last_ok},
                },
            }
        }
        try:
            self._ddb.transact_write_items(TransactItems=[update, *condition_checks])
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            }:
                return False
            raise SessionOwnershipCheckError(candidate_rca_id) from exc

    def claim_incident(
        self,
        alarm: IncidentAlarm,
        *,
        cooldown_seconds: int,
    ) -> IncidentClaim:
        candidate_rca_id = build_rca_id(build_idempotency_key(alarm))
        if not self._enabled:
            return IncidentClaim(
                IncidentClaimDisposition.CONTENDED,
                candidate_rca_id,
                reason="incident store is unavailable",
            )
        event_time = _event_time(alarm)
        cooldown = timedelta(seconds=max(cooldown_seconds, 0))

        for _ in range(_INCIDENT_CLAIM_RETRIES):
            try:
                incident = self._get_incident(alarm)
            except ClientError as exc:
                raise SessionOwnershipCheckError(candidate_rca_id) from exc
            if not incident:
                opened = self._open_first_incident(
                    alarm,
                    candidate_rca_id=candidate_rca_id,
                    event_time=event_time,
                )
                if opened:
                    return opened
                continue

            current_candidate = incident.get("candidate_rca_id", {}).get("S", "")
            last_ok_raw = incident.get("last_ok_at", {}).get("S", "")
            if not current_candidate:
                if not last_ok_raw:
                    return IncidentClaim(
                        IncidentClaimDisposition.CONTENDED,
                        candidate_rca_id,
                        reason="active incident record is invalid",
                    )
                try:
                    last_ok = datetime.fromisoformat(last_ok_raw)
                except ValueError:
                    return IncidentClaim(
                        IncidentClaimDisposition.CONTENDED,
                        candidate_rca_id,
                        reason="active incident recovery timestamp is invalid",
                    )
                if event_time < last_ok + cooldown:
                    return IncidentClaim(
                        IncidentClaimDisposition.SUPPRESSED,
                        candidate_rca_id,
                        0,
                        "alarm event-time is within the recovery cooldown",
                    )
                if self._open_from_recovery_watermark(
                    alarm,
                    recovery_at=last_ok_raw,
                    candidate_rca_id=candidate_rca_id,
                    event_time=event_time,
                ):
                    return IncidentClaim(
                        IncidentClaimDisposition.PROCEED,
                        candidate_rca_id,
                        1,
                    )
                continue

            try:
                generation = int(incident.get("generation", {}).get("N", "0"))
            except (TypeError, ValueError):
                return IncidentClaim(
                    IncidentClaimDisposition.CONTENDED,
                    candidate_rca_id,
                    reason="active incident generation is invalid",
                )
            if generation < 1:
                return IncidentClaim(
                    IncidentClaimDisposition.CONTENDED,
                    candidate_rca_id,
                    reason="active incident record is invalid",
                )
            if current_candidate == candidate_rca_id:
                self._touch_last_alarm(
                    alarm,
                    candidate_rca_id=candidate_rca_id,
                    event_time=event_time,
                )
                return IncidentClaim(IncidentClaimDisposition.PROCEED, candidate_rca_id, generation)

            opened_at_raw = incident.get("opened_at", {}).get("S", "")
            try:
                opened_at = datetime.fromisoformat(opened_at_raw)
            except ValueError:
                return IncidentClaim(
                    IncidentClaimDisposition.CONTENDED,
                    candidate_rca_id,
                    generation,
                    "active incident opened timestamp is invalid",
                )
            if event_time <= opened_at:
                return self._suppressed_incident(
                    alarm,
                    candidate_rca_id=current_candidate,
                    generation=generation,
                    event_time=event_time,
                    reason="alarm event-time does not follow the active incident",
                )

            last_ok: datetime | None = None
            if last_ok_raw:
                try:
                    last_ok = datetime.fromisoformat(last_ok_raw)
                except ValueError:
                    return IncidentClaim(
                        IncidentClaimDisposition.CONTENDED,
                        candidate_rca_id,
                        generation,
                        "active incident recovery timestamp is invalid",
                    )
                if event_time < last_ok + cooldown:
                    return self._suppressed_incident(
                        alarm,
                        candidate_rca_id=current_candidate,
                        generation=generation,
                        event_time=event_time,
                        reason="recovery cooldown has not elapsed",
                    )

            active, active_reason = self._candidate_activity(current_candidate)
            if active:
                return self._suppressed_incident(
                    alarm,
                    candidate_rca_id=current_candidate,
                    generation=generation,
                    event_time=event_time,
                    reason=active_reason,
                    retryable=True,
                )

            if last_ok is None:
                return self._suppressed_incident(
                    alarm,
                    candidate_rca_id=current_candidate,
                    generation=generation,
                    event_time=event_time,
                    reason="incident has no recovery observation",
                    retryable=True,
                )
            if self._advance_incident(
                alarm,
                previous_candidate=current_candidate,
                previous_generation=generation,
                previous_last_ok=last_ok_raw,
                candidate_rca_id=candidate_rca_id,
                event_time=event_time,
            ):
                return IncidentClaim(
                    IncidentClaimDisposition.PROCEED,
                    candidate_rca_id,
                    generation + 1,
                )

        return IncidentClaim(
            IncidentClaimDisposition.CONTENDED,
            candidate_rca_id,
            reason="active incident changed during claim",
        )

    def record_recovery(self, alarm: IncidentAlarm) -> bool:
        if not self._enabled:
            return False
        recovery_at = _iso(_event_time(alarm))
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_incident_key(alarm),
                UpdateExpression=(
                    "SET alarm_identity = :identity, alarm_name = :name, "
                    "last_ok_at = :recovery, created_at = if_not_exists(created_at, :now), "
                    "updated_at = :now, #ttl = :ttl"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(opened_at) OR opened_at <= :recovery) AND "
                    "(attribute_not_exists(last_ok_at) OR last_ok_at < :recovery)"
                ),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":identity": {"S": build_alarm_identity(alarm)},
                    ":name": {"S": alarm.alarm_name},
                    ":recovery": {"S": recovery_at},
                    ":now": {"S": _now_iso()},
                    ":ttl": {"N": str(self._incident_ttl())},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return True
            raise SessionOwnershipCheckError(alarm.alarm_name) from exc

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
        message_id: str | None = None,
        alarm_data: dict | None = None,
    ) -> SessionClaim:
        receive_count = max(receive_count, 1)
        message_id = message_id or f"direct:{rca_id}"
        claim_token = uuid.uuid4().hex
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return SessionClaim(ClaimDisposition.CONTENDED)
        now = _now_iso()
        ttl = _ttl()
        item = {
            "PK": {"S": f"RCA#{rca_id}"},
            "SK": {"S": _ANALYSIS_SESSION_SK},
            "rca_id": {"S": rca_id},
            "idempotency_key": {"S": idempotency_key},
            "alarm_name": {"S": alarm_name},
            "state": {"S": "ALARM_RECEIVED"},
            "engine": {"S": ENGINE},
            "claim_token": {"S": claim_token},
            "receive_count": {"N": str(receive_count)},
            "message_id": {"S": message_id},
            "created_at": {"S": now},
            "updated_at": {"S": now},
            # Keys for the session-list index. They duplicate engine and
            # created_at on purpose: the index must contain sessions only, and
            # hypothesis and execution items in this partition carry those same
            # two attributes. An index key written by nothing else keeps them out.
            "list_engine": {"S": ENGINE},
            "list_created_at": {"S": now},
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
        previous_message_id_attr = existing.get("message_id")
        previous_message_id = previous_message_id_attr.get("S") if previous_message_id_attr else None
        if previous_message_id_attr and not previous_message_id:
            return SessionClaim(ClaimDisposition.CONTENDED)
        if previous_message_id is not None and previous_message_id != message_id:
            return SessionClaim(ClaimDisposition.CONTENDED)
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
        if previous_message_id_attr:
            condition += " AND message_id = :previous_message_id"
            expression_values[":previous_message_id"] = {"S": previous_message_id}
        else:
            condition += " AND attribute_not_exists(message_id)"

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
                Key=_session_key(rca_id),
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
        playbook: dict | None = None,
        confirmed: bool = False,
        claim_token: str,
        side_effect_lease_token: str | None = None,
    ) -> None:
        if not DYNAMODB_TABLE_NAME or not self._ddb:
            return
        self._validate_transition(rca_id, "COMPLETED", claim_token)
        condition = _TERMINAL_COND
        current_playbook = playbook or {}
        values = {
            ":state": {"S": "COMPLETED"},
            ":rc": {"S": root_cause},
            ":report_s3_key": {"S": report_s3_key},
            ":playbook": {"S": json.dumps(current_playbook, ensure_ascii=False)},
            ":playbook_id": {"S": str(current_playbook.get("playbook_id", ""))[:200]},
            ":confirmed": {"BOOL": confirmed},
            ":now": {"S": _now_iso()},
            ":completed": {"S": "COMPLETED"},
            ":failed": {"S": "FAILED"},
            ":outdated": {"S": "OUTDATED"},
            ":cancelled": {"S": "CANCELLED"},
            ":claim": {"S": claim_token},
        }
        update = (
            "SET #state = :state, root_cause = :rc, report_s3_key = :report_s3_key, "
            "playbook = :playbook, playbook_id = :playbook_id, confirmed = :confirmed, updated_at = :now "
            "REMOVE side_effect_lease_token, side_effect_lease_name, side_effect_lease_expires_at"
        )
        if side_effect_lease_token:
            condition += " AND side_effect_lease_token = :lease"
            values[":lease"] = {"S": side_effect_lease_token}
        try:
            self._ddb.update_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key=_session_key(rca_id),
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
                Key=_session_key(rca_id),
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
                Key=_session_key(rca_id),
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
                Key=_session_key(rca_id),
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
                Key=_session_key(rca_id),
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
