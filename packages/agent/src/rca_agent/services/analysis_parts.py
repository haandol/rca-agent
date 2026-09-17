"""Persist private analysis parts without making the parent analysis or public library complete."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

WORKFLOW = "recovery-first-v1"
PARTS = ("recovery", "root_cause", "operations")
TERMINAL = ("COMPLETED", "FAILED", "SKIPPED")


def json_bytes(value: dict) -> bytes:
    """Produce deterministic artifact bytes; hashes bind exact content, not model-supplied references."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def approval_digest(playbook: dict) -> str:
    """Hash the complete approval object using the dashboard's sorted JSON representation."""

    def encode(value):
        """Match JSON.stringify number and UTF-16 key ordering without introducing runtime dependencies."""
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, (int, float)):
            import math
            from decimal import Decimal

            if not math.isfinite(value) or (isinstance(value, int) and abs(value) > 2**53 - 1):
                raise ValueError("unsupported approval numeric value")
            if value == 0:
                return "0"
            text = repr(value).lower()
            if 1e-6 <= abs(value) < 1e21:
                return (
                    format(Decimal(text), "f").rstrip("0").rstrip(".")
                    if "." in format(Decimal(text), "f")
                    else format(Decimal(text), "f")
                )
            mantissa, exponent = text.split("e") if "e" in text else (text, "0")
            power = int(exponent)
            return mantissa.rstrip("0").rstrip(".") + "e" + ("+" if power >= 0 else "-") + str(abs(power))
        if isinstance(value, list):
            return "[" + ",".join(encode(item) for item in value) + "]"
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            return (
                "{"
                + ",".join(encode(key) + ":" + encode(value[key]) for key in sorted(value, key=property_order))
                + "}"
            )
        raise ValueError("unsupported approval JSON value")

    def property_order(key):
        """JSON.stringify emits canonical uint32 property indices before other sorted UTF-16 keys."""
        if re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < 2**32 - 1:
            return (0, int(key))
        return (1, key.encode("utf-16-be"))

    return hashlib.sha256(encode(playbook).encode()).hexdigest()


RECOVERY_READ_OPERATIONS = frozenset(
    {
        ("ecs", "describe-services"),
        ("ecs", "list-tasks"),
        ("ecs", "describe-tasks"),
        ("ecs", "describe-task-definition"),
        ("cloudwatch", "list-metrics"),
        ("cloudwatch", "describe-alarms"),
        ("cloudwatch", "get-metric-data"),
        ("cloudwatch", "get-metric-statistics"),
        ("logs", "filter-log-events"),
        ("logs", "get-log-events"),
    }
)


def validate_recovery_operations(playbook: dict) -> None:
    """Permit exactly one pinned rollback and known bounded reads, never another mutation in early recovery."""
    from .runbook_contract import SCOPE_FIELDS, _command_parts, rollback_argv, validate_runbook

    steps = playbook.get("execution_steps", [])
    validate_runbook(steps)
    context = playbook.get("rollback_context")
    if not isinstance(context, dict):
        raise ValueError("early recovery requires verified rollback context")
    try:
        raw_scope, current, normal = context["scope"], context["current"], context["normal"]
        scope = {**raw_scope, "cluster": raw_scope["cluster_arn"], "service": raw_scope["service_arn"]}
        guard = {
            **{key: scope[key] for key in SCOPE_FIELDS},
            "expected_task_definition": current["task_definition_arn"],
            "expected_image_digest": current["image_digest"],
            "expected_deployment_id": current["deployment_id"],
            "service_settings": context["service_settings"],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError("early recovery context is incomplete") from exc
    allowed_flags = {
        ("ecs", "describe-services"): {"--cluster", "--services", "--include"},
        ("ecs", "list-tasks"): {"--cluster", "--service-name", "--desired-status"},
        ("ecs", "describe-tasks"): {"--cluster", "--tasks", "--include"},
        ("ecs", "describe-task-definition"): {"--task-definition", "--include"},
        ("cloudwatch", "list-metrics"): {"--namespace", "--metric-name", "--dimensions"},
        ("cloudwatch", "describe-alarms"): {"--alarm-names"},
        ("cloudwatch", "get-metric-data"): {"--metric-data-queries", "--start-time", "--end-time", "--scan-by"},
        ("cloudwatch", "get-metric-statistics"): {
            "--namespace",
            "--metric-name",
            "--dimensions",
            "--start-time",
            "--end-time",
            "--period",
            "--statistics",
            "--unit",
        },
        ("logs", "filter-log-events"): {
            "--log-group-name",
            "--log-stream-names",
            "--start-time",
            "--end-time",
            "--filter-pattern",
            "--limit",
        },
        ("logs", "get-log-events"): {"--log-group-name", "--log-stream-name", "--start-time", "--end-time", "--limit"},
    }
    writes = []
    for index, step in enumerate(steps):
        for command in step.get("commands", []):
            argv, region = _command_parts(command)
            operation = tuple(argv[1:3])
            if argv[:3] == ["aws", "ecs", "update-service"]:
                options = rollback_argv(command)
                if step.get("ecs_service_precondition") != guard or options != {
                    "--cluster": scope["cluster"],
                    "--service": scope["service"],
                    "--region": scope["region"],
                    "--task-definition": normal["task_definition_arn"],
                }:
                    raise ValueError("early rollback differs from pinned context")
                writes.append(step["step_id"])
                continue
            if argv[:1] != ["aws"] or operation not in RECOVERY_READ_OPERATIONS or region != scope["region"]:
                raise ValueError(f"step {index}: early recovery permits pinned UpdateService and recognized reads only")
            options, flag = {}, None
            for token in argv[3:]:
                if token.startswith("--"):
                    flag, _, inline = token.partition("=")
                    if flag in options or flag not in allowed_flags[operation] | {"--query", "--output"}:
                        raise ValueError(f"step {index}: unsupported or duplicate early read option")
                    options[flag] = [inline] if inline else []
                elif flag and token not in {";", "&&", "||", "|", ">", "<"}:
                    options[flag].append(token)
                else:
                    raise ValueError(f"step {index}: invalid early read arguments")
            if any(not values for values in options.values()):
                raise ValueError("early read options require fixed values")
            if "--output" in options and options["--output"] != ["json"]:
                raise ValueError("early read output must remain JSON")
            if (
                operation[0] == "ecs"
                and operation[1] != "describe-task-definition"
                and options.get("--cluster") not in ([scope["cluster"]], [scope["cluster"].rsplit("/", 1)[-1]])
            ):
                raise ValueError("early read cluster differs from pinned context")
            if operation == ("ecs", "describe-services") and options.get("--services") not in (
                [scope["service"]],
                [raw_scope.get("service_name", scope["service"].rsplit("/", 1)[-1])],
            ):
                raise ValueError("early read service differs from pinned context")
            if operation == ("ecs", "list-tasks") and options.get("--service-name") not in (
                [scope["service"]],
                [raw_scope.get("service_name", scope["service"].rsplit("/", 1)[-1])],
            ):
                raise ValueError("early task enumeration must name the pinned service")
            if operation == ("ecs", "describe-task-definition") and options.get("--task-definition") not in (
                [normal["task_definition_arn"]],
                [current["task_definition_arn"]],
            ):
                raise ValueError("early definition read differs from pinned context")
            if operation[0] == "logs" and options.get("--log-group-name") != [raw_scope.get("log_group")]:
                raise ValueError("early log group differs from pinned context")
            if (operation[0] == "logs" or operation[1] in {"get-metric-data", "get-metric-statistics"}) and (
                not options.get("--start-time") or not options.get("--end-time")
            ):
                raise ValueError("early observation read requires a fixed bounded time window")
    if len(writes) != 1:
        raise ValueError("early recovery requires exactly one pinned UpdateService")
    for step in steps:
        wait = step.get("deployment_wait")
        if wait and (
            wait["action_step_id"] != writes[0]
            or any(wait[key] != scope[key] for key in SCOPE_FIELDS)
            or wait["task_definition"] != normal["task_definition_arn"]
            or wait["image_digest"] != normal["image_digest"]
        ):
            raise ValueError("early deployment wait differs from pinned rollback")
        if step.get("metric_wait") and not step["metric_wait"].get("deployment_step_id"):
            raise ValueError("early metrics must follow the approved deployment wait")


def _pack(value: dict) -> dict:
    """Encode lightweight metadata for the same low-level DynamoDB transaction as the parent guard."""
    serializer = TypeSerializer()
    return {key: serializer.serialize(item) for key, item in value.items()}


def _unpack(value: dict) -> dict:
    """Decode metadata without treating unverified object content as authority."""
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(item) for key, item in value.items()}


class AnalysisPartStore:
    """Bind immutable S3 bodies to ordered, claim-fenced private analysis metadata."""

    def __init__(self, ddb, s3, *, table_name: str, bucket: str, engine: str, clock=time.time):
        """Require configured storage and a fixed server engine before accepting any publication."""
        if not table_name or not bucket or engine not in {"strands", "headless-codex"}:
            raise ValueError("analysis part storage requires table, bucket and canonical engine")
        self.ddb, self.s3 = ddb, s3
        self.table_name, self.bucket, self.engine, self.clock = table_name, bucket, engine, clock

    def _key(self, rca_id: str, suffix: str) -> dict:
        """Derive keys only from validated server identity, never from an output URL."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", rca_id):
            raise ValueError("invalid analysis RCA identity")
        return {"PK": f"RCA#{rca_id}", "SK": suffix}

    def _get(self, rca_id: str, suffix: str) -> dict | None:
        """Read current authority consistently before retry, resume or conditional replacement."""
        item = self.ddb.get_item(
            TableName=self.table_name, Key=_pack(self._key(rca_id, suffix)), ConsistentRead=True
        ).get("Item")
        return _unpack(item) if item else None

    def _guard(self, rca_id: str, claim_token: str) -> dict:
        """Fence every commit against cancellation, deletion, expiry and claim replacement."""
        return {
            "ConditionCheck": {
                "TableName": self.table_name,
                "Key": _pack(self._key(rca_id, "ANALYSIS#SESSION")),
                "ConditionExpression": "#claim = :claim AND #engine = :engine AND #ttl > :now "
                "AND NOT (#state IN (:done, :failed, :cancelled, :outdated)) AND attribute_not_exists(#deleting)",
                "ExpressionAttributeNames": {
                    "#state": "state",
                    "#ttl": "ttl",
                    "#claim": "claim_token",
                    "#engine": "engine",
                    "#deleting": "deleting_at",
                },
                "ExpressionAttributeValues": _pack(
                    {
                        ":claim": claim_token,
                        ":engine": self.engine,
                        ":now": int(self.clock()),
                        ":done": "COMPLETED",
                        ":failed": "FAILED",
                        ":cancelled": "CANCELLED",
                        ":outdated": "OUTDATED",
                    }
                ),
            }
        }

    def _check_claim(self, rca_id: str, claim_token: str) -> None:
        """Even duplicate submissions must belong to the current live writer."""
        self.ddb.transact_write_items(TransactItems=[self._guard(rca_id, claim_token)])

    def _put_object(self, key: str, raw: bytes) -> None:
        """Create content once; a retry succeeds only when existing bytes are exactly identical."""
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw, ContentType="application/json", IfNoneMatch="*")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                raise
            if self._read_object(key) != raw:
                raise ValueError("immutable analysis object differs") from error

    def _read_object(self, key: str) -> bytes:
        """Close read bodies so verification does not retain an unused transport resource."""
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def _body(self, record: dict, part: str) -> dict:
        """Verify expiry, deterministic key, hash and payload identity before exposing any result."""
        rca_id = record.get("rca_id", "")
        origin_engine = record.get("engine")
        if (
            origin_engine not in {"strands", "headless-codex"}
            or (part != "incident" and origin_engine != self.engine)
            or record.get("workflow") != WORKFLOW
        ):
            raise ValueError("analysis record identity mismatch")
        if int(record.get("body_expires_at", 0)) <= int(self.clock()):
            raise ValueError("analysis payload expired")
        digest, key = record.get("payload_sha256"), record.get("payload_s3_key")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid analysis payload hash")
        if key != f"analysis-parts/{origin_engine}/{rca_id}/{part}/{digest}.json":
            raise ValueError("analysis payload key mismatch")
        raw = self._read_object(key)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("analysis payload hash mismatch")
        payload = json.loads(raw)
        if (
            payload.get("rca_id") != rca_id
            or payload.get("engine") != origin_engine
            or payload.get("schema_version") != 1
        ):
            raise ValueError("analysis payload identity mismatch")
        if part != "incident" and (
            payload.get("part") != part
            or payload.get("workflow") != WORKFLOW
            or payload.get("status") != record.get("status")
            or record.get("revision") != digest
            or payload.get("incident_ref")
            != {"key": record.get("incident_s3_key"), "sha256": record.get("incident_sha256")}
        ):
            raise ValueError("analysis part envelope mismatch")
        if part == "recovery" and record.get("approval_status") == "READY":
            result = payload.get("result", {})
            book = result.get("playbook")
            if (
                payload.get("status") != "COMPLETED"
                or result.get("recommendation") != "ROLLBACK"
                or result.get("verification", {}).get("valid") is not True
                or not isinstance(book, dict)
                or record.get("runbook_digest") != approval_digest(book)
            ):
                raise ValueError("stored READY recovery metadata is inconsistent")
            validate_recovery_operations(book)
        return payload

    def read_incident(self, rca_id: str) -> dict | None:
        """Resume the original frozen input; missing and unreadable snapshots are not interchangeable."""
        record = self._get(rca_id, "INCIDENT_SNAPSHOT")
        if record is None:
            return None
        if record.get("rca_id") != rca_id:
            raise ValueError("incident record RCA mismatch")
        return {"record": record, "payload": self._body(record, "incident")}

    def freeze_incident(self, rca_id: str, claim_token: str, attempt: int, incident: dict) -> dict:
        """Persist the first incident before publication; retries cannot replace evidence after rollback."""
        self._check_claim(rca_id, claim_token)
        payload = {**incident, "schema_version": 1, "rca_id": rca_id, "engine": self.engine}
        if set(payload) != {
            "schema_version",
            "rca_id",
            "engine",
            "alarm",
            "scoping",
            "observations",
            "source_artifacts",
        }:
            raise ValueError("invalid frozen incident fields")
        existing = self.read_incident(rca_id)
        if existing:
            if existing["record"]["engine"] != self.engine:
                return existing
            if json_bytes(existing["payload"]) != json_bytes(payload):
                raise ValueError("incident snapshot already frozen")
            return existing
        raw = json_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        now = int(self.clock())
        key = f"analysis-parts/{self.engine}/{rca_id}/incident/{digest}.json"
        self._put_object(key, raw)
        record = {
            **self._key(rca_id, "INCIDENT_SNAPSHOT"),
            "schema_version": 1,
            "workflow": WORKFLOW,
            "rca_id": rca_id,
            "engine": self.engine,
            "payload_s3_key": key,
            "payload_sha256": digest,
            "claim_token": claim_token,
            "attempt": attempt,
            "created_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "body_expires_at": now + 60 * 86400,
            "ttl": now + 90 * 86400,
        }
        parent = self._guard(rca_id, claim_token)["ConditionCheck"]
        parent["UpdateExpression"] = "SET #workflow = :workflow"
        parent["ExpressionAttributeNames"]["#workflow"] = "workflow"
        parent["ConditionExpression"] += " AND (attribute_not_exists(#workflow) OR #workflow = :workflow)"
        parent["ExpressionAttributeValues"].update(_pack({":workflow": WORKFLOW}))
        self.ddb.transact_write_items(
            TransactItems=[
                {"Update": parent},
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": _pack(record),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
            ]
        )
        return {"record": record, "payload": payload}

    def read_part(self, rca_id: str, part: str) -> dict | None:
        """Return verified published content; RUNNING metadata cannot masquerade as a completed result."""
        if part not in PARTS:
            raise ValueError("unknown analysis part")
        record = self._get(rca_id, f"{self.engine}#ANALYSIS_PART#{part}")
        if record is None:
            return None
        if record.get("rca_id") != rca_id or record.get("engine") != self.engine or record.get("part") != part:
            raise ValueError("part record identity mismatch")
        return {"record": record, "payload": self._body(record, part) if record.get("revision") else None}

    def _previous_guard(self, rca_id: str, part: str) -> list[dict]:
        """Require a persisted predecessor result, regardless of recovery approval or execution state."""
        index = PARTS.index(part)
        if not index:
            return []
        prior = self.read_part(rca_id, PARTS[index - 1])
        if not prior or prior["record"].get("status") not in TERMINAL or prior["payload"] is None:
            raise ValueError("previous analysis part has no published result")
        return [
            {
                "ConditionCheck": {
                    "TableName": self.table_name,
                    "Key": _pack(self._key(rca_id, f"{self.engine}#ANALYSIS_PART#{PARTS[index - 1]}")),
                    "ConditionExpression": "revision = :revision",
                    "ExpressionAttributeValues": _pack({":revision": prior["record"]["revision"]}),
                }
            }
        ]

    def start_part(self, rca_id: str, part: str, claim_token: str, attempt: int) -> dict:
        """Register actual work under the active claim; completed parts are reused on redelivery."""
        self._check_claim(rca_id, claim_token)
        if part not in PARTS:
            raise ValueError("unknown analysis part")
        incident = self.read_incident(rca_id)
        if incident is None:
            raise ValueError("incident must be frozen before starting a part")
        current = self.read_part(rca_id, part)
        if current and current["record"]["status"] in TERMINAL:
            return current
        now = int(self.clock())
        stamp = datetime.fromtimestamp(now, UTC).isoformat()
        prior = current["record"] if current else {}
        record = {
            **self._key(rca_id, f"{self.engine}#ANALYSIS_PART#{part}"),
            "schema_version": 1,
            "workflow": WORKFLOW,
            "rca_id": rca_id,
            "engine": self.engine,
            "part": part,
            "status": "RUNNING",
            "claim_token": claim_token,
            "attempt": attempt,
            "created_at": prior.get("created_at", stamp),
            "updated_at": stamp,
            "started_at": stamp,
            "incident_s3_key": incident["record"]["payload_s3_key"],
            "incident_sha256": incident["record"]["payload_sha256"],
            "body_expires_at": prior.get("body_expires_at", now + 60 * 86400),
            "ttl": prior.get("ttl", now + 90 * 86400),
            "summary": "",
            "error": "",
        }
        self.ddb.transact_write_items(
            TransactItems=[
                self._guard(rca_id, claim_token),
                *self._previous_guard(rca_id, part),
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": _pack(record),
                        "ConditionExpression": (
                            "attribute_not_exists(PK) OR (#status = :running AND attribute_not_exists(revision))"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _pack({":running": "RUNNING"}),
                    }
                },
            ]
        )
        return {"record": record, "payload": None}

    def publish_part(
        self,
        rca_id: str,
        part: str,
        claim_token: str,
        attempt: int,
        *,
        result: dict,
        status: str = "COMPLETED",
        limitations: list | None = None,
        error: str = "",
        input_refs: list | None = None,
        approval_status: str = "UNAVAILABLE",
        runbook_digest: str = "",
        expected_revision: str | None = None,
    ) -> dict:
        """Publish only persisted, identity-bound results; explicit revision CAS preserves prior approvals."""
        self._check_claim(rca_id, claim_token)
        if part not in PARTS or status not in TERMINAL or (status != "COMPLETED" and not error.strip()):
            raise ValueError("invalid part result status or missing failure reason")
        incident = self.read_incident(rca_id)
        if incident is None:
            raise ValueError("frozen incident unavailable")
        current = self.read_part(rca_id, part)
        if not current:
            if status != "SKIPPED":
                raise ValueError("part was not started")
            now = int(self.clock())
            stamp = datetime.fromtimestamp(now, UTC).isoformat()
            base = {
                **self._key(rca_id, f"{self.engine}#ANALYSIS_PART#{part}"),
                "schema_version": 1,
                "workflow": WORKFLOW,
                "rca_id": rca_id,
                "engine": self.engine,
                "part": part,
                "created_at": stamp,
                "body_expires_at": now + 60 * 86400,
                "ttl": now + 90 * 86400,
            }
        else:
            base = current["record"]
        incident_ref = {"key": incident["record"]["payload_s3_key"], "sha256": incident["record"]["payload_sha256"]}
        payload = {
            "schema_version": 1,
            "workflow": WORKFLOW,
            "rca_id": rca_id,
            "engine": self.engine,
            "part": part,
            "status": status,
            "incident_ref": incident_ref,
            "input_refs": input_refs or [],
            "result": result,
            "limitations": limitations or [],
        }
        if error:
            payload["error"] = error
        raw = json_bytes(payload)
        revision = hashlib.sha256(raw).hexdigest()
        if base.get("revision") and base["revision"] != revision and expected_revision != base["revision"]:
            raise ValueError("part replacement requires expected current revision")
        if expected_revision is not None and base.get("revision") != expected_revision:
            raise ValueError("analysis revision conflict")
        if part == "recovery" and approval_status not in {"READY", "UNAVAILABLE", "REVOKED"}:
            raise ValueError("invalid recovery approval status")
        if (
            part == "recovery"
            and approval_status == "READY"
            and (
                status != "COMPLETED"
                or result.get("recommendation") != "ROLLBACK"
                or not result.get("playbook")
                or result.get("verification", {}).get("valid") is not True
                or not re.fullmatch(r"[a-f0-9]{64}", runbook_digest)
                or result["playbook"].get("rca_id") != rca_id
                or runbook_digest != approval_digest(result["playbook"])
            )
        ):
            raise ValueError("READY requires server verification and a canonical runbook digest")
        if part == "recovery" and approval_status == "READY":
            validate_recovery_operations(result["playbook"])
        if current and base.get("revision") == revision:
            if part == "recovery" and (
                base.get("approval_status") != approval_status
                or (approval_status == "READY" and base.get("runbook_digest") != runbook_digest)
            ):
                raise ValueError("duplicate recovery metadata differs")
            return current
        key = f"analysis-parts/{self.engine}/{rca_id}/{part}/{revision}.json"
        self._put_object(key, raw)
        record = {
            **base,
            "status": status,
            "revision": revision,
            "payload_s3_key": key,
            "payload_sha256": revision,
            "incident_s3_key": incident_ref["key"],
            "incident_sha256": incident_ref["sha256"],
            "claim_token": claim_token,
            "attempt": attempt,
            "updated_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            "completed_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            "summary": str(result.get("summary", ""))[:500],
            "error": error[:500],
        }
        if part == "recovery":
            record["approval_status"] = approval_status
            if approval_status == "READY":
                record["runbook_digest"] = runbook_digest
            else:
                record.pop("runbook_digest", None)
        condition = "revision = :old" if base.get("revision") else "attribute_not_exists(revision)"
        values = _pack({":old": base["revision"]}) if base.get("revision") else None
        put = {"TableName": self.table_name, "Item": _pack(record), "ConditionExpression": condition}
        if values:
            put["ExpressionAttributeValues"] = values
        version = {**record, "SK": f"{self.engine}#ANALYSIS_PART_VERSION#{part}#{revision}"}
        self.ddb.transact_write_items(
            TransactItems=[
                self._guard(rca_id, claim_token),
                *self._previous_guard(rca_id, part),
                {"Put": put},
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": _pack(version),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
            ]
        )
        return {"record": record, "payload": payload}

    def complete_analysis(
        self,
        rca_id: str,
        claim_token: str,
        *,
        notification: dict,
        report_s3_key: str = "",
        playbook: dict | None = None,
        playbook_metric_name: str = "",
        playbook_span_id: str = "",
        side_effect_lease_token: str | None = None,
    ) -> bool:
        """Finish the parent only after all three immutable outcomes, without publishing private runbooks."""
        reads = [self.read_part(rca_id, part) for part in PARTS]
        if any(not item or item["record"].get("status") not in TERMINAL for item in reads):
            raise ValueError("all analysis parts must publish before parent completion")
        if notification.get("rca_id") != rca_id:
            raise ValueError("completion notification identity mismatch")
        root_result = reads[1]["payload"].get("result", {})
        root = root_result.get("root_cause", {})
        parent = self._guard(rca_id, claim_token)["ConditionCheck"]
        parent["ConditionExpression"] += " AND #workflow = :workflow"
        parent["ExpressionAttributeNames"].update(
            {
                "#workflow": "workflow",
                "#lease": "side_effect_lease_token",
                "#lease_expires": "side_effect_lease_expires_at",
                "#lease_claim": "side_effect_lease_claim_token",
                "#lease_name": "side_effect_lease_name",
            }
        )
        if side_effect_lease_token is not None:
            if not side_effect_lease_token:
                raise ValueError("completion lease token must not be empty")
            parent["ConditionExpression"] += (
                " AND #lease = :lease AND #lease_expires > :now "
                "AND (attribute_not_exists(#lease_claim) OR #lease_claim = :claim)"
            )
            parent["ExpressionAttributeValues"].update(_pack({":lease": side_effect_lease_token}))
        else:
            parent["ConditionExpression"] += (
                " AND ((attribute_not_exists(#lease) AND attribute_not_exists(#lease_expires)) "
                "OR #lease_expires <= :now)"
            )
        parent["UpdateExpression"] = (
            "SET #state = :final, completed_at = :stamp, updated_at = :stamp, root_cause = :cause, "
            "confirmed = :confirmed, selected_hypothesis_id = :selected, "
            "completion_notification = :notification, completion_notification_status = :pending, "
            "severity = :severity, fault_type = :fault, analysis_parts_finalized = :finalized"
        )
        parent["ExpressionAttributeValues"].update(
            _pack(
                {
                    ":workflow": WORKFLOW,
                    ":finalized": True,
                    ":final": "COMPLETED" if reads[1]["record"]["status"] == "COMPLETED" else "FAILED",
                    ":stamp": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
                    ":cause": root.get("description", "Unknown"),
                    ":confirmed": bool(root.get("confirmed", False)),
                    ":selected": root.get("selected_hypothesis_id", ""),
                    ":notification": json_bytes(notification).decode(),
                    ":pending": "PENDING",
                    ":severity": root_result.get("report", {}).get("severity", notification.get("severity", "medium")),
                    ":fault": root_result.get("validated_fault_type", "UNSUPPORTED"),
                }
            )
        )
        if report_s3_key:
            parent["UpdateExpression"] += ", report_s3_key = :report"
            parent["ExpressionAttributeValues"].update(_pack({":report": report_s3_key}))
        if playbook is not None and reads[1]["record"]["status"] == "COMPLETED":
            if playbook.get("rca_id") != rca_id or not playbook.get("playbook_id"):
                raise ValueError("final public knowledge identity mismatch")
            parent["UpdateExpression"] += (
                ", completion_playbook = :book, playbook_id = :book_id, playbook_index_status = :pending, "
                "completion_playbook_metric_name = :metric, playbook_span_id = :span"
            )
            parent["ExpressionAttributeValues"].update(
                _pack(
                    {
                        ":book": json_bytes(playbook).decode(),
                        ":book_id": playbook["playbook_id"],
                        ":metric": playbook_metric_name,
                        ":span": playbook_span_id,
                    }
                )
            )
        checks = []
        for item in reads:
            record = item["record"]
            checks.append(
                {
                    "ConditionCheck": {
                        "TableName": self.table_name,
                        "Key": _pack({key: record[key] for key in ("PK", "SK")}),
                        "ConditionExpression": "revision = :revision",
                        "ExpressionAttributeValues": _pack({":revision": record["revision"]}),
                    }
                }
            )
        for name in (
            "severity",
            "fault_type",
            "analysis_parts_finalized",
            "completed_at",
            "updated_at",
            "root_cause",
            "confirmed",
            "selected_hypothesis_id",
            "completion_notification",
            "completion_notification_status",
            "report_s3_key",
            "completion_playbook",
            "playbook_id",
            "playbook_index_status",
            "completion_playbook_metric_name",
            "playbook_span_id",
        ):
            if re.search(r"(?<![A-Za-z0-9_#])" + name + r"(?= =)", parent["UpdateExpression"]):
                alias = "#u_" + name
                parent["UpdateExpression"] = re.sub(
                    r"(?<![A-Za-z0-9_#])" + name + r"(?= =)", alias, parent["UpdateExpression"]
                )
                parent["ExpressionAttributeNames"][alias] = name
        parent["UpdateExpression"] += " REMOVE #lease, #lease_expires, #lease_claim, #lease_name"
        self.ddb.transact_write_items(TransactItems=[{"Update": parent}, *checks])
        return True
