"""Canonical playbook storage; mirrored in both engines and checked by parity tests.

Vectors are disposable pointers. Only a retained completed source and the exact
published head authorize a read; a broken head never resurrects an older value.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

_ENGINES = {"strands", "headless-codex", "codex-headless", "cc-headless"}
_ANNOTATIONS = {"library_revision", "source_engine", "source_rca_id"}
_AUXILIARY = _ANNOTATIONS | {"comparison", "stage", "summary", "output_summary"}


def payload(value: dict) -> dict:
    """Remove lookup annotations, preserving the entire domain/comparison payload."""
    return {key: item for key, item in value.items() if key not in _ANNOTATIONS}


def encoded(value: dict) -> str:
    """Compare full JSON values independently of object key insertion order."""
    return json.dumps(payload(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def same_knowledge(left: dict, right: dict) -> bool:
    """Match the dashboard's full domain comparison, retaining commands and all unknown domain fields."""
    return encoded({key: value for key, value in left.items() if key not in _AUXILIARY}) == encoded(
        {key: value for key, value in right.items() if key not in _AUXILIARY}
    )


def matched_comparison(value: dict) -> bool:
    """A matched incident proposes knowledge changes; completion cannot apply them."""
    comparison = value.get("comparison") or {}
    return (
        isinstance(comparison, dict)
        and comparison.get("status") in {"UPDATE_PROPOSED", "NO_CHANGE"}
        and comparison.get("selected_playbook_id") == value.get("playbook_id")
    )


def _pack(value: dict) -> dict:
    """Use low-level DynamoDB values so all mutations can share one transaction."""
    serializer = TypeSerializer()
    return {key: serializer.serialize(item) for key, item in value.items()}


def _unpack(value: dict) -> dict:
    """Decode only actual items, never treating a client/mock response as content."""
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(item) for key, item in value.items()}


def _live(item: dict) -> bool:
    """TTL deletion is asynchronous, so readers enforce retention themselves."""
    try:
        return int(item.get("ttl", 0)) > int(time.time())
    except (ValueError, TypeError, OverflowError):
        return False


ORIGINAL_SECONDS = 60 * 86400
STATE_SECONDS = 90 * 86400
_STATE_FIELDS = (
    "source_rca_id",
    "engine",
    "revision",
    "publication_status",
    "vector_key",
    "metric_name",
    "updated_at",
)


def _epoch(value) -> int:
    """Read recorded timestamps without inventing a new origin for an old body."""
    if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("original timestamp lacks timezone")
        return int(stamp.timestamp())
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError("invalid original timestamp")
    return int(value)


def original_created(item: dict, *, legacy: bool = False) -> int:
    """Prefer explicit birth timestamps, then the recorded update time of first-version heads."""
    timestamps = [item[name] for name in ("original_created_at", "completed_at", "created_at") if name in item]
    if not timestamps and "updated_at" in item:
        timestamps = [item["updated_at"]]
    origin = (
        min(_epoch(value) for value in timestamps)
        if timestamps
        else int(item["ttl"]) - (STATE_SECONDS if legacy else ORIGINAL_SECONDS)
    )
    if origin > int(time.time()):
        raise ValueError("original timestamp is in the future")
    return origin


def original_live(item: dict, *, legacy: bool = False) -> bool:
    """Body eligibility ends at sixty days even while its ninety-day state survives."""
    try:
        return _live(item) and original_created(item, legacy=legacy) + ORIGINAL_SECONDS > int(time.time())
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def legacy_origin(item: dict, *, retrospective: bool = False, parent: dict | None = None) -> int:
    """Use the current body's own timestamp, never a newer enclosing session update."""
    fields = (
        ("original_created_at", "updated_at", "created_at", "completed_at")
        if retrospective
        else ("original_created_at", "completed_at", "created_at", "updated_at")
    )
    for field in fields:
        if field in item:
            origin = _epoch(item[field])
            if origin > int(time.time()):
                raise ValueError("legacy original timestamp is in the future")
            return origin
    if "ttl" in item:
        return int(item["ttl"]) - STATE_SECONDS
    if parent is not None:
        return legacy_origin(parent)
    raise ValueError("legacy original has no retention lineage")


def legacy_original_live(item: dict, *, retrospective: bool = False, parent: dict | None = None) -> bool:
    """Old inline originals expire at sixty days; their source state still lives for ninety."""
    try:
        return ("ttl" not in item or _live(item)) and legacy_origin(
            item, retrospective=retrospective, parent=parent
        ) + ORIGINAL_SECONDS > int(time.time())
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def state_record(head: dict) -> dict:
    """Retain only list/publication metadata for ninety days, never body or comparison inputs."""
    body = json.loads(head["playbook_json"])
    return {
        "PK": "PLAYBOOK_LIBRARY_STATE",
        "SK": head["SK"],
        **{key: head.get(key, "") for key in _STATE_FIELDS},
        **{
            key: body.get(key, [] if key == "tags" else "")
            for key in ("failure_type", "symptom_pattern", "tags", "verification_status")
        },
        "ttl": original_created(head) + STATE_SECONDS,
    }


def thin_comparison(comparison: dict, original_sk: str, expires_at: int) -> dict:
    """Keep only disposition and immutable-original references in ninety-day incident state."""
    result = {
        "status": comparison.get("status", "SEARCH_FAILED"),
        "selected_playbook_id": comparison.get("selected_playbook_id", ""),
        "original_sk": original_sk,
        "original_expires_at": expires_at,
    }
    proposal = comparison.get("proposal")
    if isinstance(proposal, dict):
        result["proposal"] = {"proposal_id": proposal["proposal_id"], "state": proposal.get("state", "PENDING")}
    return result


def _body(raw: object, playbook_id: str) -> dict | None:
    """Reject malformed or misidentified bodies rather than guessing their fields."""
    try:
        result = json.loads(raw) if isinstance(raw, str) else None
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict) or result.get("playbook_id") != playbook_id:
        return None
    if not isinstance(result.get("failure_type"), str) or not isinstance(result.get("symptom_pattern"), str):
        return None
    status = result.get("verification_status", "DRAFT")
    if not isinstance(status, str) or status not in {"DRAFT", "VERIFIED"}:
        return None
    for name in ("severity_criteria", "temporary_mitigation", "permanent_remediation", "escalation_criteria", "rca_id"):
        if name in result and not isinstance(result[name], str):
            return None
    for name in ("tags", "verification_steps", "prevention_measures", "related_metrics"):
        if name in result and (
            not isinstance(result[name], list) or any(not isinstance(value, str) for value in result[name])
        ):
            return None
    steps = result.get("execution_steps", [])
    if not isinstance(steps, list):
        return None
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("step_id"), str) or not step["step_id"].strip():
            return None
        if any(name in step and not isinstance(step[name], str) for name in ("intent", "action", "success_criteria")):
            return None
        commands = step.get("commands", [])
        if not isinstance(commands, list) or any(not isinstance(command, str) for command in commands):
            return None
        if step.get("metric_wait") is not None and not isinstance(step["metric_wait"], dict):
            return None
    if result.get("comparison") is not None and not isinstance(result["comparison"], dict):
        return None
    return result


def _json_number(value):
    """Legacy DynamoDB maps use Decimal while domain JSON uses ordinary numbers."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError("not a JSON value")


class PlaybookLibrary:
    """Own canonical CAS, immutable snapshots and strict legacy read compatibility."""

    def __init__(self, client, table: str, ttl_days: int = 90):
        """Keep configuration outside the helper so both engines use the same wire."""
        self.client, self.table, self.ttl_days = client, table, ttl_days

    def _get(self, pk: str, sk: str) -> dict | None:
        """Read one authority record consistently without conflating absence and malformed data."""
        if not self.client or not self.table:
            raise RuntimeError("playbook state storage unavailable")
        item = self.client.get_item(TableName=self.table, Key=_pack({"PK": pk, "SK": sk}), ConsistentRead=True).get(
            "Item"
        )
        return _unpack(item) if isinstance(item, dict) and item else None

    def head(self, playbook_id: str) -> dict | None:
        """Expired bodies and surviving state are barriers, never permission to revive legacy knowledge."""
        body = self._get("PLAYBOOK_LIBRARY", playbook_id)
        if body is not None:
            if original_live(body):
                return body
            return {
                "PK": "PLAYBOOK_LIBRARY",
                "SK": playbook_id,
                "_body_unavailable": True,
                **{key: body.get(key, "") for key in _STATE_FIELDS},
                "ttl": body.get("ttl", 0),
            }
        state = self._get("PLAYBOOK_LIBRARY_STATE", playbook_id)
        if state is not None and _live(state):
            return {**state, "PK": "PLAYBOOK_LIBRARY", "_body_unavailable": True}
        return None

    def archive_comparison(self, playbook: dict, rca_id: str, engine: str) -> dict:
        """Freeze every comparison input before completion and return only its sixty-day reference."""
        comparison = playbook.get("comparison")
        if not isinstance(comparison, dict) or not comparison:
            return deepcopy(playbook)
        sessions = [
            item
            for item in self.records(rca_id)
            if item.get("SK") == "ANALYSIS#SESSION"
            or item.get("SK") == "SESSION"
            or item.get("SK", "").endswith("#SESSION")
        ]
        sessions = [item for item in sessions if self.engine(item) == engine]
        if len(sessions) != 1 or "deleting_at" in sessions[0] or not _live(sessions[0]):
            raise ValueError("comparison source is missing, expired or being deleted")
        session = sessions[0]
        prefix = f"{engine}#PLAYBOOK_COMPARISON#"
        original_sk = comparison.get("original_sk")
        if original_sk:
            if not isinstance(original_sk, str) or not original_sk.startswith(prefix):
                raise ValueError("comparison reference belongs to a different source")
            recorded = self._get(f"RCA#{rca_id}", original_sk)
            if recorded is None or not original_live(recorded):
                raise ValueError("comparison original is unavailable")
            frozen = json.loads(recorded["comparison_json"])
            thin = thin_comparison(frozen, original_sk, int(recorded["ttl"]))
            if comparison != thin:
                raise ValueError("comparison reference changed its immutable disposition")
        else:
            raw = json.dumps(comparison, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            comparison_id = hashlib.sha256(raw.encode()).hexdigest()
            original_sk = prefix + comparison_id
            recorded = self._get(f"RCA#{rca_id}", original_sk)
            if recorded is None:
                now = int(time.time())
                recorded = {
                    "PK": f"RCA#{rca_id}",
                    "SK": original_sk,
                    "comparison_id": comparison_id,
                    "comparison_json": raw,
                    "created_at": datetime.fromtimestamp(now, UTC).isoformat(),
                    "ttl": now + ORIGINAL_SECONDS,
                    "engine": engine,
                    "source_rca_id": rca_id,
                }
                try:
                    self.client.transact_write_items(
                        TransactItems=[
                            {
                                "ConditionCheck": {
                                    "TableName": self.table,
                                    "Key": _pack({"PK": session["PK"], "SK": session["SK"]}),
                                    "ConditionExpression": "attribute_exists(PK) AND attribute_not_exists(deleting_at) "
                                    "AND #ttl = :observed AND #ttl > :now",
                                    "ExpressionAttributeNames": {"#ttl": "ttl"},
                                    "ExpressionAttributeValues": _pack({":observed": session["ttl"], ":now": now}),
                                }
                            },
                            {
                                "Put": {
                                    "TableName": self.table,
                                    "Item": _pack(recorded),
                                    "ConditionExpression": "attribute_not_exists(PK)",
                                }
                            },
                        ]
                    )
                except Exception:
                    recorded = self._get(f"RCA#{rca_id}", original_sk)
                    if recorded is None:
                        raise
            if recorded.get("comparison_json") != raw or not original_live(recorded):
                raise ValueError("immutable comparison expired or changed")
            thin = thin_comparison(comparison, original_sk, int(recorded["ttl"]))
        return {**deepcopy(playbook), "comparison": thin}

    def retrospective_baseline(self, target: dict, rca_id: str) -> dict:
        """Resolve the approved public revision independently of this incident's runbook.

        A matched incident changes its RCA identity and execution plan without
        changing the public revision it observed. Its immutable public snapshot,
        not the incident plan, is the compare-and-swap baseline.
        """
        playbook_id = target.get("playbook_id", "")
        head = self.head(playbook_id)
        expected = target.get("library_revision", "")
        if head is None:
            if expected not in {"", "legacy"}:
                raise ValueError("approved public baseline disappeared")
            return target
        if expected in {"", "legacy"}:
            expected = f"analysis:{rca_id}"
            if head.get("source_rca_id") != rca_id:
                raise ValueError("incident has no approved revision for this public head")
        if head.get("revision") != expected or head.get("publication_status") != "PUBLISHED" or not _live(head):
            raise ValueError("approved public baseline is no longer current")
        source = self.source(head.get("source_rca_id", ""), playbook_id, head.get("engine", ""))
        if source is None:
            raise ValueError("public baseline source unavailable")
        raw = self.client.get_item(
            TableName=self.table,
            Key=_pack({"PK": f"PLAYBOOK#{playbook_id}", "SK": expected}),
            ConsistentRead=True,
        ).get("Item")
        snapshot = _unpack(raw) if isinstance(raw, dict) and raw else {}
        if (
            not original_live(snapshot)
            or snapshot.get("revision") != expected
            or snapshot.get("playbook_json") != head.get("playbook_json")
            or snapshot.get("source_rca_id") != head.get("source_rca_id")
            or snapshot.get("engine") != head.get("engine")
        ):
            raise ValueError("immutable public baseline is missing or differs from current head")
        body = _body(snapshot.get("playbook_json"), playbook_id)
        if body is None:
            raise ValueError("immutable public baseline is malformed")
        return {
            **body,
            "library_revision": expected,
            "source_engine": head["engine"],
            "source_rca_id": head["source_rca_id"],
        }

    def records(self, rca_id: str) -> list[dict]:
        """Read every legacy/source page; a later page may own the current revision."""
        if not self.client or not self.table or not rca_id:
            return []
        request = {
            "TableName": self.table,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": f"RCA#{rca_id}"}},
            "ConsistentRead": True,
        }
        items = []
        while True:
            result = self.client.query(**request)
            items.extend(_unpack(item) for item in result.get("Items", []))
            cursor = result.get("LastEvaluatedKey")
            if not cursor:
                return items
            request["ExclusiveStartKey"] = cursor

    def source(self, rca_id: str, playbook_id: str, engine: str = "") -> tuple[dict, list[dict]] | None:
        """Only completed, retained sources without a deletion claim authorize publication."""
        items = self.records(rca_id)
        sessions = [item for item in items if item.get("SK") == "ANALYSIS#SESSION"]
        if not sessions:
            sessions = [
                item for item in items if item.get("SK") == "SESSION" or item.get("SK", "").endswith("#SESSION")
            ]
            if engine:
                sessions = [item for item in sessions if self.engine(item) == engine]
        if len(sessions) != 1:
            return None
        session = sessions[0]
        actual_engine = self.engine(session)
        if not actual_engine or (engine and actual_engine != engine):
            return None
        if session.get("state") != "COMPLETED" or not _live(session) or "deleting_at" in session:
            return None
        if session.get("playbook_id") != playbook_id:
            return None
        return session, items

    @staticmethod
    def engine(item: dict) -> str:
        """Prefer recorded ownership, inferring only unambiguous engine-prefixed keys."""
        engine = item.get("engine") or ("strands" if item.get("SK") == "SESSION" else item.get("SK", "").split("#")[0])
        return engine if engine in _ENGINES else ""

    def legacy(self, rca_id: str, playbook_id: str, engine: str = "", publication_id: str = "") -> dict | None:
        """Load the published legacy revision or completed original, never a broken latest."""
        source = self.source(rca_id, playbook_id, engine)
        if source is None:
            return None
        session, items = source
        engine = self.engine(session)
        if session.get("playbook_index_status", "PUBLISHED") != "PUBLISHED":
            return None
        revisions = [item for item in items if item.get("SK") == f"{engine}#PLAYBOOK_REVISION"]
        if revisions:
            revision = revisions[0]
            if revision.get("publication_status") != "PUBLISHED" or ("ttl" in revision and not _live(revision)):
                return None
            if publication_id and revision.get("revised_by_execution_id") != publication_id:
                return None
            token = revision.get("revised_by_execution_id", "")
            if token and self.snapshot(playbook_id, f"retrospective:{token}") is not None:
                # A managed retrospective must finish its canonical head CAS first.
                return None
            if not legacy_original_live(revision, retrospective=True, parent=session):
                return None
            result = _body(revision.get("playbook"), playbook_id)
        else:
            # A vector for an uncommitted retrospective cannot identify the original.
            if publication_id:
                return None
            raw = session.get("completion_playbook", session.get("playbook"))
            if raw is not None:
                if not legacy_original_live(session):
                    return None
                result = _body(raw, playbook_id)
            else:
                spans = [
                    item
                    for item in items
                    if item.get("span_type") == "PLAYBOOK"
                    and self.engine(item) == engine
                    and legacy_original_live(item, parent=session)
                    and isinstance(item.get("metadata"), dict)
                    and item["metadata"].get("playbook_id") == playbook_id
                ]
                result = (
                    _body(json.dumps(spans[0]["metadata"], default=_json_number), playbook_id)
                    if len(spans) == 1
                    else None
                )
        if result is None:
            return None
        return {**result, "library_revision": "legacy", "source_rca_id": rca_id, "source_engine": engine}

    def load(self, playbook_id: str, rca_id: str, engine: str = "", revision: str = "", publication_id: str = ""):
        """Only the exact published canonical head may satisfy an indexed candidate."""
        head = self.head(playbook_id)
        if head is None:
            if revision and revision != "legacy":
                return None
            return self.legacy(rca_id, playbook_id, engine, publication_id)
        if head.get("_body_unavailable") or head.get("publication_status") != "PUBLISHED" or not original_live(head):
            return None
        if not revision or revision != head.get("revision"):
            return None
        if head.get("vector_key") != f"{playbook_id}@{revision}":
            return None
        if rca_id != head.get("source_rca_id") or (engine and engine != head.get("engine")):
            return None
        if not self.source(rca_id, playbook_id, head.get("engine", "")):
            return None
        result = _body(head.get("playbook_json"), playbook_id)
        if result is None:
            return None
        return {**result, "library_revision": revision, "source_rca_id": rca_id, "source_engine": head["engine"]}

    def _source_condition(self, session: dict) -> dict:
        """Fence deletion, expiry, ownership and identity changes in the write transaction."""
        values = {":state": "COMPLETED", ":now": int(time.time()), ":id": session["playbook_id"]}
        names = {"#state": "state", "#ttl": "ttl"}
        condition = "#state = :state AND #ttl > :now AND playbook_id = :id AND attribute_not_exists(deleting_at)"
        if session.get("engine"):
            condition += " AND #engine = :engine"
            names["#engine"] = "engine"
            values[":engine"] = session["engine"]
        for number, field in enumerate(("original_created_at", "completed_at", "created_at")):
            alias = f"#origin{number}"
            names[alias] = field
            if field in session:
                value = f":origin{number}"
                condition += f" AND {alias} = {value}"
                values[value] = session[field]
            else:
                condition += f" AND attribute_not_exists({alias})"
        for field in ("completion_playbook", "playbook"):
            if field in session:
                condition += " AND #body = :body"
                names["#body"] = field
                values[":body"] = session[field]
                break
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": _pack({"PK": session["PK"], "SK": session["SK"]}),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": _pack(values),
            }
        }

    def snapshot(self, playbook_id: str, revision: str) -> dict | None:
        """Read immutable publication work independently of the current head."""
        item = self.client.get_item(
            TableName=self.table,
            Key=_pack({"PK": f"PLAYBOOK#{playbook_id}", "SK": revision}),
            ConsistentRead=True,
        ).get("Item")
        if not isinstance(item, dict) or not item:
            return None
        snapshot = _unpack(item)
        return {**snapshot, "PK": "PLAYBOOK_LIBRARY", "SK": playbook_id} if original_live(snapshot) else None

    def stage(
        self,
        playbook: dict,
        rca_id: str,
        revision: str,
        *,
        engine: str = "",
        metric_name: str = "",
        baseline: dict | None = None,
        publication_result: dict | None = None,
    ) -> dict:
        """Stage analyses with their head; stage retrospectives without replacing public knowledge."""
        playbook_id = playbook.get("playbook_id", "")
        source = self.source(rca_id, playbook_id, engine)
        if source is None:
            raise ValueError("completed retained playbook source unavailable")
        session, items = source
        body = encoded(playbook)
        origin = int(time.time()) if baseline is not None else legacy_origin(session)
        if _body(body, playbook_id) is None:
            raise ValueError("malformed playbook")
        current = self.head(playbook_id)
        if current and current.get("revision") == revision:
            if (
                encoded(json.loads(current.get("playbook_json", ""))) != body
                or current.get("source_rca_id") != rca_id
                or current.get("engine") != self.engine(session)
                or current.get("publication_status") not in {"PENDING", "PUBLISHED"}
                or current.get("vector_key") != f"{playbook_id}@{revision}"
                or not _live(current)
            ):
                raise ValueError("publication replay changed content or source")
            return current
        actions = [self._source_condition(session)]
        head_condition = {"TableName": self.table, "ConditionExpression": "attribute_not_exists(PK)"}
        if baseline is None:
            if not legacy_original_live(session):
                raise ValueError("completed analysis original expired")
            if current is not None:
                raise ValueError("new analysis cannot replace an existing head")
            original = session.get("completion_playbook", session.get("playbook"))
            if original is None or encoded(json.loads(original)) != body:
                raise ValueError("analysis differs from completed source")
        elif current is None:
            if baseline.get("library_revision", "legacy") != "legacy":
                raise ValueError("canonical baseline disappeared")
            prior = self.legacy(rca_id, playbook_id, self.engine(session))
            if prior is None or not same_knowledge(prior, baseline):
                raise ValueError("legacy retrospective baseline changed")
            revision_key = f"{self.engine(session)}#PLAYBOOK_REVISION"
            recorded = next((item for item in items if item.get("SK") == revision_key), None)
            guard = {
                "TableName": self.table,
                "Key": _pack({"PK": f"RCA#{rca_id}", "SK": revision_key}),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
            if recorded:
                guard.update(
                    ConditionExpression="playbook = :body AND publication_status = :status "
                    "AND revised_by_execution_id = :execution",
                    ExpressionAttributeValues=_pack(
                        {
                            ":body": recorded["playbook"],
                            ":status": "PUBLISHED",
                            ":execution": recorded["revised_by_execution_id"],
                        }
                    ),
                )
            actions.append({"ConditionCheck": guard})
        else:
            if (
                current.get("revision") != baseline.get("library_revision")
                or not same_knowledge(json.loads(current.get("playbook_json", "")), baseline)
                or current.get("publication_status") != "PUBLISHED"
                or not _live(current)
            ):
                raise ValueError("retrospective baseline changed")
            old_source = self.source(current.get("source_rca_id", ""), playbook_id, current.get("engine", ""))
            if old_source is None:
                raise ValueError("public baseline source unavailable")
            if (old_source[0]["PK"], old_source[0]["SK"]) != (session["PK"], session["SK"]):
                actions.append(self._source_condition(old_source[0]))
            head_condition.update(
                ConditionExpression="#revision = :revision AND playbook_json = :body AND publication_status = :status "
                "AND source_rca_id = :source AND #engine = :engine AND #ttl > :now",
                ExpressionAttributeNames={"#revision": "revision", "#engine": "engine", "#ttl": "ttl"},
                ExpressionAttributeValues=_pack(
                    {
                        ":revision": current["revision"],
                        ":body": current["playbook_json"],
                        ":status": "PUBLISHED",
                        ":source": current["source_rca_id"],
                        ":engine": current["engine"],
                        ":now": int(time.time()),
                    }
                ),
            )
        head = {
            "PK": "PLAYBOOK_LIBRARY",
            "SK": playbook_id,
            "playbook_json": body,
            "revision": revision,
            "source_rca_id": rca_id,
            "engine": self.engine(session),
            "publication_status": "PENDING",
            "vector_key": f"{playbook_id}@{revision}",
            "metric_name": metric_name,
            "updated_at": datetime.now(UTC).isoformat(),
            "original_created_at": datetime.fromtimestamp(origin, UTC).isoformat(),
            "ttl": origin + ORIGINAL_SECONDS,
        }
        if publication_result is not None:
            head["retrospective_result"] = publication_result
        if baseline is not None:
            head.update(
                baseline_revision=current["revision"] if current else "legacy",
                baseline_playbook_json=current["playbook_json"] if current else encoded(baseline),
                baseline_absent=current is None,
                previous_vector_key=current.get("vector_key", playbook_id) if current else playbook_id,
            )
            # Exact retry resumes this immutable work; it cannot rebase onto a new head.
            previous = self.snapshot(playbook_id, revision)
            if previous:
                if any(
                    previous.get(key) != head[key]
                    for key in (
                        "playbook_json",
                        "source_rca_id",
                        "engine",
                        "baseline_revision",
                        "baseline_playbook_json",
                        "baseline_absent",
                        "vector_key",
                    )
                ) or not _live(previous):
                    raise ValueError("retrospective publication replay changed its immutable snapshot")
                return previous
            actions.append(
                {"ConditionCheck": {**head_condition, "Key": _pack({"PK": "PLAYBOOK_LIBRARY", "SK": playbook_id})}}
            )
        else:
            actions.append({"Put": {**head_condition, "Item": _pack(head)}})
            actions.append(
                {
                    "Put": {
                        "TableName": self.table,
                        "Item": _pack(state_record(head)),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
        actions.append(
            {
                "Put": {
                    "TableName": self.table,
                    "Item": _pack({**head, "PK": f"PLAYBOOK#{playbook_id}", "SK": revision}),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        )
        self.client.transact_write_items(TransactItems=actions)
        return head

    def committed(self, head: dict) -> dict | None:
        """Retrospective publication requires its original revision commit, not its stage."""
        source = self.source(head["source_rca_id"], head["SK"], head["engine"])
        if source is None:
            return None
        for item in source[1]:
            if item.get("SK") != f"{head['engine']}#PLAYBOOK_REVISION":
                continue
            parsed = _body(item.get("playbook"), head["SK"])
            if (
                item.get("publication_status") == "PUBLISHED"
                and item.get("revised_by_execution_id") == head["revision"].removeprefix("retrospective:")
                and parsed is not None
                and encoded(parsed) == head["playbook_json"]
                and ("ttl" not in item or _live(item))
            ):
                return item
        return None

    def abandon(self, work: dict) -> None:
        """Finish superseded publication work as failed without touching the newer public head."""
        if not work.get("retrospective_result"):
            return
        execution_id = work["revision"].removeprefix("retrospective:")
        key = {"PK": f"RCA#{work['source_rca_id']}", "SK": f"EXEC#{execution_id}"}
        raw = self.client.get_item(TableName=self.table, Key=_pack(key), ConsistentRead=True).get("Item")
        item = _unpack(raw) if isinstance(raw, dict) and raw else {}
        if item.get("retrospective_status") != "RUNNING":
            return
        self.client.update_item(
            TableName=self.table,
            Key=_pack(key),
            UpdateExpression="SET retrospective_status = :failed, retrospective_summary = :reason",
            ConditionExpression="claim_token = :claim AND execution_state = :resolved "
            "AND retrospective_status = :running",
            ExpressionAttributeValues=_pack(
                {
                    ":claim": item["claim_token"],
                    ":resolved": "RESOLVED",
                    ":running": "RUNNING",
                    ":failed": "FAILED",
                    ":reason": "Public baseline changed before publication; preserved the newer library revision.",
                }
            ),
        )

    def finalize(self, head: dict) -> None:
        """CAS a committed retrospective into the head; failures leave prior public knowledge intact."""
        source = self.source(head["source_rca_id"], head["SK"], head["engine"])
        if source is None or not original_live(head):
            raise ValueError("publication source expired")
        current = self.head(head["SK"])
        if (
            current
            and current.get("revision") == head["revision"]
            and current.get("playbook_json") == head["playbook_json"]
            and current.get("publication_status") == "PUBLISHED"
        ):
            return
        origin = original_created(head)
        original_expiry = min(int(head["ttl"]), origin + ORIGINAL_SECONDS)
        adopt_original_retention = "original_created_at" not in head or int(head["ttl"]) != original_expiry
        head = {
            **head,
            "original_created_at": datetime.fromtimestamp(origin, UTC).isoformat(),
            "ttl": original_expiry,
        }
        actions = [self._source_condition(source[0])]
        if head["revision"].startswith("retrospective:"):
            committed = self.committed(head)
            if committed is None:
                raise ValueError("retrospective revision not committed")
            actions.append(
                {
                    "ConditionCheck": {
                        "TableName": self.table,
                        "Key": _pack({"PK": committed["PK"], "SK": committed["SK"]}),
                        "ConditionExpression": "publication_status = :status AND revised_by_execution_id = :id "
                        "AND playbook = :body",
                        "ExpressionAttributeValues": _pack(
                            {
                                ":status": "PUBLISHED",
                                ":id": committed["revised_by_execution_id"],
                                ":body": committed["playbook"],
                            }
                        ),
                    }
                }
            )
            put = {
                "TableName": self.table,
                "Item": _pack({**head, "publication_status": "PUBLISHED"}),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
            if not head.get("baseline_absent"):
                put.update(
                    ConditionExpression="#revision = :revision AND playbook_json = :body "
                    "AND publication_status = :published",
                    ExpressionAttributeNames={"#revision": "revision"},
                    ExpressionAttributeValues=_pack(
                        {
                            ":revision": head["baseline_revision"],
                            ":body": head["baseline_playbook_json"],
                            ":published": "PUBLISHED",
                        }
                    ),
                )
            actions.append({"Put": put})
        else:
            actions.append(
                {
                    "Update": {
                        "TableName": self.table,
                        "Key": _pack({"PK": "PLAYBOOK_LIBRARY", "SK": head["SK"]}),
                        "UpdateExpression": "SET publication_status = :published, #ttl = :original_expiry, "
                        "original_created_at = :origin",
                        "ConditionExpression": "#revision = :revision AND playbook_json = :body AND #ttl > :now",
                        "ExpressionAttributeNames": {"#revision": "revision", "#ttl": "ttl"},
                        "ExpressionAttributeValues": _pack(
                            {
                                ":revision": head["revision"],
                                ":body": head["playbook_json"],
                                ":published": "PUBLISHED",
                                ":now": int(time.time()),
                                ":original_expiry": head["ttl"],
                                ":origin": head["original_created_at"],
                            }
                        ),
                    }
                }
            )
        if adopt_original_retention:
            # Compatibility is read-only; only this requested publication shortens old snapshot metadata.
            actions.append(
                {
                    "Update": {
                        "TableName": self.table,
                        "Key": _pack({"PK": f"PLAYBOOK#{head['SK']}", "SK": head["revision"]}),
                        "UpdateExpression": "SET #ttl = :expiry, original_created_at = :origin",
                        "ConditionExpression": "#revision = :revision AND playbook_json = :body",
                        "ExpressionAttributeNames": {"#ttl": "ttl", "#revision": "revision"},
                        "ExpressionAttributeValues": _pack(
                            {
                                ":expiry": head["ttl"],
                                ":origin": head["original_created_at"],
                                ":revision": head["revision"],
                                ":body": head["playbook_json"],
                            }
                        ),
                    }
                }
            )
        published_state = state_record({**head, "publication_status": "PUBLISHED"})
        expected_revision = (
            head.get("baseline_revision") if head["revision"].startswith("retrospective:") else head["revision"]
        )
        state_put = {
            "TableName": self.table,
            "Item": _pack(published_state),
            "ConditionExpression": "attribute_not_exists(PK)",
        }
        if expected_revision != "legacy":
            state_put.update(
                ConditionExpression="attribute_not_exists(PK) OR #revision = :expected",
                ExpressionAttributeNames={"#revision": "revision"},
                ExpressionAttributeValues=_pack({":expected": expected_revision}),
            )
        actions.append({"Put": state_put})
        result = head.get("retrospective_result")
        if result:
            execution_id = head["revision"].removeprefix("retrospective:")
            raw = self.client.get_item(
                TableName=self.table,
                Key=_pack({"PK": f"RCA#{head['source_rca_id']}", "SK": f"EXEC#{execution_id}"}),
                ConsistentRead=True,
            ).get("Item")
            execution = _unpack(raw) if isinstance(raw, dict) and raw else {}
            if not execution.get("claim_token"):
                raise ValueError("retrospective publication owner unavailable")
            actions.append(
                {
                    "Update": {
                        "TableName": self.table,
                        "Key": _pack({"PK": execution["PK"], "SK": execution["SK"]}),
                        "UpdateExpression": "SET retrospective_status = :status, retrospective_summary = :summary, "
                        "playbook_snapshot_s3_key = :snapshot, retrospective_diff_s3_key = :diff",
                        "ConditionExpression": "claim_token = :claim AND execution_state = :resolved "
                        "AND retrospective_status = :running",
                        "ExpressionAttributeValues": _pack(
                            {
                                ":claim": execution["claim_token"],
                                ":resolved": "RESOLVED",
                                ":running": "RUNNING",
                                ":status": result["status"],
                                ":summary": result["summary"],
                                ":snapshot": result["playbook_snapshot_s3_key"],
                                ":diff": result["diff_s3_key"],
                            }
                        ),
                    }
                }
            )
        self.client.transact_write_items(TransactItems=actions)
