"""Resume retained private reviews without invoking execution or a model."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime

from botocore.exceptions import ClientError

from headless_codex.adapters.secondary.playbook.library import _pack, _unpack, original_live
from headless_codex.services.playbook_merge import (
    apply_retrospective_verification,
    merge_playbook_update,
    procedure_identity,
)

_MAX_SOURCE_BYTES = 32 * 1024 * 1024


def _unique_object(pairs):
    """Reject duplicate JSON keys instead of selecting one conflicting authority value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate retained JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    """Reject nonfinite JSON numbers in retained evidence."""
    raise ValueError("nonfinite retained JSON number")


def content_hash(value: dict) -> str:
    """Bind domain JSON independently of transport whitespace without replacing approval byte hashes."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class DeferredPublication:
    """Use the existing table's idle scan and publication CAS; the approval queue stays unchanged."""

    def __init__(self, ddb, s3, table, bucket, execution_store, playbook_store, *, clock=time.time):
        """Keep publication-only dependencies and bounded scan cursors, without any execution/model runner."""
        self.ddb, self.s3, self.table, self.bucket = ddb, s3, table, bucket
        self.execution_store, self.playbook_store = execution_store, playbook_store
        self.clock = clock
        self.cursor = None
        self.work_cursor = None
        self.pending = deque()
        self.discovery_turn = 0

    def _get(self, key):
        """Read one authoritative row consistently."""
        raw = self.ddb.get_item(TableName=self.table, Key=_pack(key), ConsistentRead=True).get("Item")
        return _unpack(raw) if raw else None

    def _read_bytes(self, key, digest=None):
        """Read bounded exact bytes; oversized, expired or ambiguous documents fail closed."""
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "NoSuchBucket", "404"}:
                raise ValueError("retained source no longer available") from exc
            raise
        try:
            raw = body.read(_MAX_SOURCE_BYTES + 1)
        finally:
            body.close()
        modified = response.get("LastModified")
        if len(raw) > _MAX_SOURCE_BYTES:
            raise ValueError("retained source exceeds bounded reader capacity")
        if response.get("ContentLength") is not None and response["ContentLength"] != len(raw):
            raise ValueError("retained source length differs from its stored metadata")
        if not isinstance(modified, datetime) or modified.timestamp() + 60 * 86400 <= self.clock():
            raise ValueError("retained source exceeded sixty-day lifetime")
        actual = hashlib.sha256(raw).hexdigest()
        if digest and actual != digest:
            raise ValueError("retained source digest changed")
        return raw, int(modified.timestamp()) + 60 * 86400

    def _read(self, key, digest=None):
        """Decode verified retained JSON without weakening byte integrity or expiry checks."""
        raw, expiry = self._read_bytes(key, digest)
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("retained source is not an object")
        return value, hashlib.sha256(raw).hexdigest(), expiry

    def _sources(self, execution):
        """Verify approval identity and sixty-day source lifetime separately from state retention."""
        rca, eid, aid = execution["rca_id"], execution["execution_id"], execution["approval_id"]
        started = datetime.fromisoformat(str(execution.get("started_at", "")).replace("Z", "+00:00"))
        if (
            execution.get("execution_state") != "RESOLVED"
            or execution.get("PK") != f"RCA#{rca}"
            or execution.get("SK") != f"EXEC#{eid}"
            or execution.get("engine") not in {"strands", "headless-codex"}
            or not re.fullmatch(r"[a-f0-9]{64}", str(execution.get("playbook_digest", "")))
            or execution.get("source_part") != "recovery"
            or int(execution.get("ttl", 0)) <= self.clock()
            or started.tzinfo is None
            or started.timestamp() + 60 * 86400 <= self.clock()
            or execution.get("approved_playbook_s3_key") != f"approvals/{rca}/{aid}/playbook.json"
            or execution.get("evidence_s3_key") != f"executions/{rca}/{eid}/evidence.json"
            or execution.get("retrospective_diff_s3_key") != f"executions/{rca}/{eid}/retrospective-diff.json"
            or execution.get("source_part_revision") != execution.get("source_part_payload_sha256")
            or not re.fullmatch(r"[a-f0-9]{64}", execution.get("source_part_revision", ""))
        ):
            raise ValueError("resolved approved recovery lineage unavailable or expired")
        approved, _, approved_expiry = self._read(execution["approved_playbook_s3_key"], execution["playbook_digest"])
        evidence, evidence_hash, evidence_expiry = self._read(execution["evidence_s3_key"])
        review, review_hash, review_expiry = self._read(execution["retrospective_diff_s3_key"])
        if (
            approved.get("rca_id") != rca
            or evidence.get("rca_id") != rca
            or evidence.get("execution_id") != eid
            or evidence.get("playbook_id") != approved.get("playbook_id")
            or evidence.get("final_state") != "RESOLVED"
            or evidence.get("resolution_confirmed") is not True
            or not isinstance(review.get("rationale"), str)
            or not review["rationale"].strip()
            or not isinstance(review.get("update"), dict)
            or not approved.get("execution_steps")
        ):
            raise ValueError("retained review or resolved evidence unavailable")
        merge_playbook_update(approved, review["update"])
        return (
            approved,
            review,
            evidence_hash,
            review_hash,
            min(approved_expiry, evidence_expiry, review_expiry, int(started.timestamp()) + 60 * 86400),
        )

    def enroll(self, rca_id: str, execution_id: str):
        """Append a deterministic child only after reading valid retained review; never rewrite EXEC history."""
        execution = self._get({"PK": f"RCA#{rca_id}", "SK": f"EXEC#{execution_id}"})
        if not execution:
            return None
        if execution.get("rca_id") != rca_id or execution.get("execution_id") != execution_id:
            raise ValueError("execution record does not match its requested identity")
        if execution.get("retrospective_status") not in {"COMPLETED", "FAILED"}:
            return None
        approved, _, evidence_hash, review_hash, source_expiry = self._sources(execution)
        key = {"PK": execution["PK"], "SK": f"RETROSPECTIVE_FOLLOWUP#{execution_id}#{review_hash}"}
        existing = self._get(key)
        if existing:
            return existing
        now = int(self.clock())
        row = {
            **key,
            "record_type": "RETROSPECTIVE_FOLLOWUP",
            "schema_version": 1,
            "attempt_id": review_hash,
            "rca_id": rca_id,
            "execution_id": execution_id,
            "engine": execution["engine"],
            "approval_id": execution["approval_id"],
            "playbook_digest": execution["playbook_digest"],
            "approved_playbook_s3_key": execution["approved_playbook_s3_key"],
            "source_part_revision": execution["source_part_revision"],
            "recovery_playbook_sha256": content_hash(approved),
            "review_s3_key": execution["retrospective_diff_s3_key"],
            "review_sha256": review_hash,
            "evidence_s3_key": execution["evidence_s3_key"],
            "evidence_sha256": evidence_hash,
            "review_status": "COMPLETED",
            "status": "WAITING_FOR_PUBLICATION",
            "reason": "canonical binding not yet available",
            "created_at": now,
            "updated_at": now,
            "ttl": int(execution["ttl"]),
            "body_expires_at": source_expiry,
        }
        work_key = {"PK": "RETROSPECTIVE_PENDING", "SK": f"{rca_id}#{execution_id}#{review_hash}"}
        row["pending_key"] = work_key
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self.table,
                            "Key": _pack({"PK": execution["PK"], "SK": execution["SK"]}),
                            "ConditionExpression": "execution_state = :resolved AND playbook_digest = :digest "
                            "AND retrospective_diff_s3_key = :review AND evidence_s3_key = :evidence "
                            "AND source_part_revision = :revision AND retrospective_status = :review_status "
                            "AND #ttl > :now",
                            "ExpressionAttributeNames": {"#ttl": "ttl"},
                            "ExpressionAttributeValues": _pack(
                                {
                                    ":resolved": "RESOLVED",
                                    ":digest": row["playbook_digest"],
                                    ":review": row["review_s3_key"],
                                    ":evidence": row["evidence_s3_key"],
                                    ":revision": row["source_part_revision"],
                                    ":review_status": execution["retrospective_status"],
                                    ":now": now,
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": _pack(row),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": _pack({**work_key, "followup_key": key, "ttl": row["ttl"]}),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            return self._get(key)
        return row

    def _finish(self, row, token, status, reason):
        """Record only the owned child result; leave original execution and review history untouched."""
        self.ddb.update_item(
            TableName=self.table,
            Key=_pack({"PK": row["PK"], "SK": row["SK"]}),
            UpdateExpression=(
                "SET #status = :status, reason = :reason, updated_at = :now REMOVE lease_token, lease_until"
            ),
            ConditionExpression="#status = :publishing AND lease_token = :token",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=_pack(
                {
                    ":status": status,
                    ":publishing": "PUBLISHING",
                    ":token": token,
                    ":reason": reason,
                    ":now": int(self.clock()),
                }
            ),
        )
        if status == "BLOCKED":
            self.ddb.delete_item(
                TableName=self.table,
                Key=_pack(row["pending_key"]),
                ConditionExpression="followup_key = :key",
                ExpressionAttributeValues=_pack({":key": {"PK": row["PK"], "SK": row["SK"]}}),
            )

    def _proposal_wait(self, draft, parent, row, approved, binding):
        """Read exact user disposition for waiting/negative proof; never manufacture a positive binding."""
        comparison = draft.get("comparison") or {}
        if comparison.get("status") != "UPDATE_PROPOSED":
            return None
        proposal = comparison.get("proposal") or {}
        proposal_id = proposal.get("proposal_id")
        public_id = comparison.get("selected_playbook_id")
        if (
            not isinstance(proposal_id, str)
            or not proposal_id
            or not isinstance(public_id, str)
            or not public_id
            or proposal.get("playbook_id", public_id) != public_id
        ):
            raise ValueError("public proposal identity unavailable")
        disposition = self._get(
            {
                "PK": row["PK"],
                "SK": f"{parent['engine']}#PLAYBOOK_PROPOSAL#{proposal_id}",
            }
        )
        if disposition is None:
            return "public knowledge proposal awaits user application"
        if disposition.get("playbook_id") != public_id or int(disposition.get("ttl", 0)) <= self.clock():
            raise ValueError("public proposal disposition differs or expired")
        state = disposition.get("state")
        if state == "REJECTED":
            raise ValueError("user rejected the public knowledge proposal")
        if state == "PENDING":
            return "public knowledge proposal awaits user application"
        if state != "APPLIED":
            raise ValueError("public proposal disposition is unsupported")
        if disposition.get("publication_status") != "PUBLISHED":
            return "applied public proposal awaits index publication"
        revision = disposition.get("result_revision")
        if not isinstance(revision, str) or not revision:
            raise ValueError("applied public proposal revision unavailable")
        library = self.playbook_store._library
        head = library.head(public_id)
        snapshot = library.snapshot(public_id, revision)
        if (
            not head
            or not snapshot
            or head.get("revision") != revision
            or head.get("publication_status") != "PUBLISHED"
            or head.get("playbook_json") != snapshot.get("playbook_json")
        ):
            raise ValueError("applied proposal canonical revision changed or unavailable")
        public = json.loads(snapshot["playbook_json"])
        if procedure_identity(public) != procedure_identity(approved):
            raise ValueError("applied proposal procedure differs from approved recovery")
        if binding and (binding.get("public_revision") != revision or binding.get("public_playbook_id") != public_id):
            raise ValueError("publication binding differs from applied proposal")
        return "applied matching public proposal awaits immutable recovery binding"

    def resume(self, row):
        """Apply one preserved review to its exact publisher-linked canonical base, or retain a reason."""
        token, now = uuid.uuid4().hex, int(self.clock())
        key = {"PK": row["PK"], "SK": row["SK"]}
        try:
            response = self.ddb.update_item(
                TableName=self.table,
                Key=_pack(key),
                UpdateExpression=(
                    "SET #status = :publishing, lease_token = :token, lease_until = :until, updated_at = :now"
                ),
                ConditionExpression="#status IN (:waiting, :failed) OR (#status = :publishing AND lease_until < :now)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_pack(
                    {
                        ":publishing": "PUBLISHING",
                        ":waiting": "WAITING_FOR_PUBLICATION",
                        ":failed": "FAILED",
                        ":token": token,
                        ":until": now + 300,
                        ":now": now,
                    }
                ),
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return
            raise
        row = _unpack(response["Attributes"])
        try:
            execution = self._get({"PK": row["PK"], "SK": f"EXEC#{row['execution_id']}"})
            approved, review, evidence_hash, review_hash, source_expiry = self._sources(execution or {})
            if (
                any(
                    execution.get(field) != row.get(field)
                    for field in (
                        "rca_id",
                        "execution_id",
                        "engine",
                        "approval_id",
                        "approved_playbook_s3_key",
                        "playbook_digest",
                        "source_part_revision",
                        "evidence_s3_key",
                    )
                )
                or evidence_hash != row["evidence_sha256"]
                or review_hash != row["review_sha256"]
                or content_hash(approved) != row["recovery_playbook_sha256"]
                or int(row["ttl"]) <= now
                or source_expiry != int(row["body_expires_at"])
                or source_expiry <= now
            ):
                raise ValueError("followup retained lineage differs or expired")
            parent = self._get({"PK": row["PK"], "SK": "ANALYSIS#SESSION"})
            if parent is None:
                parent = self._get({"PK": row["PK"], "SK": f"{row['engine']}#SESSION"})
            if (
                parent is None
                or parent.get("state") in {"FAILED", "CANCELLED", "OUTDATED", "DELETING"}
                or any(field in parent for field in ("deleting", "deleting_at", "deletion_requested_at", "deleted_at"))
                or int(parent.get("ttl", 0)) <= now
                or not original_live(parent, legacy=True)
            ):
                raise ValueError("analysis source unavailable, failed, deleting or expired")
            wait_reason = "canonical binding not yet available"
            draft = None
            if parent.get("state") == "COMPLETED":
                raw_public = parent.get("completion_playbook", parent.get("playbook"))
                draft = (
                    json.loads(raw_public, object_pairs_hook=_unique_object)
                    if isinstance(raw_public, str)
                    else raw_public
                )
                if not isinstance(draft, dict) or not draft.get("playbook_id"):
                    raise ValueError("completed analysis has no retained public draft")
                index_state = parent.get("playbook_index_status", "")
                if (
                    index_state not in {"PENDING", "PUBLISHED"}
                    and (draft.get("comparison") or {}).get("status") != "UPDATE_PROPOSED"
                ):
                    raise ValueError("completed public draft has no valid publication handoff")
                wait_reason = (
                    "retained public draft awaits index publication"
                    if index_state == "PENDING"
                    else "published public draft awaits exact recovery binding"
                )
            binding_key = {"PK": row["PK"], "SK": f"RECOVERY_PUBLICATION#{row['source_part_revision']}"}
            binding = self._get(binding_key)
            proposal_wait = self._proposal_wait(draft, parent, row, approved, binding) if draft else None
            if proposal_wait:
                wait_reason = proposal_wait
                if binding and "awaits immutable" not in proposal_wait:
                    raise ValueError("public binding exists before user application completed")
            if binding is None:
                from headless_codex.services.legacy_recovery_association import inspect_association

                candidate = inspect_association(self, row, parent, draft or {}, approved)
                if candidate:
                    binding, _, guards = candidate
                    binding = {**binding_key, **binding}
                    try:
                        self.ddb.transact_write_items(
                            TransactItems=[
                                self._lease_guard(row, token),
                                *guards,
                                {
                                    "Put": {
                                        "TableName": self.table,
                                        "Item": _pack(binding),
                                        "ConditionExpression": "attribute_not_exists(PK)",
                                    }
                                },
                            ]
                        )
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] != "TransactionCanceledException":
                            raise
                        existing = self._get(binding_key)
                        if not existing or any(existing.get(k) != v for k, v in binding.items() if k != "created_at"):
                            raise ValueError("legacy association raced a changed authority") from exc
                        binding = existing
                else:
                    self._finish(row, token, "WAITING_FOR_PUBLICATION", wait_reason)
                    return
            if (
                binding.get("schema_version") != 1
                or binding.get("rca_id") != row["rca_id"]
                or binding.get("engine") != row["engine"]
                or binding.get("recovery_revision") != row["source_part_revision"]
                or binding.get("recovery_playbook_sha256") != content_hash(approved)
                or int(binding.get("ttl", 0)) <= now
            ):
                raise ValueError("publisher binding differs or expired")
            library = self.playbook_store._library
            public_id, revision = binding["public_playbook_id"], binding["public_revision"]
            baseline = library.snapshot(public_id, revision)
            if not baseline or not original_live(baseline):
                raise ValueError("canonical snapshot missing or expired")
            public = json.loads(baseline["playbook_json"])
            related_public = public
            if binding.get("association_mode") == "LEGACY_SAME_GENERATION":
                from headless_codex.services.legacy_recovery_association import inspect_association

                candidate = inspect_association(self, row, parent, draft or {}, approved)
                if not candidate:
                    raise ValueError("retained legacy association no longer verifiable")
                expected, related_public, _ = candidate
                if any(binding.get(k) != v for k, v in expected.items() if k not in {"created_at", "ttl"}):
                    raise ValueError("legacy association source changed")
                if int(binding["ttl"]) > int(expected["ttl"]):
                    raise ValueError("legacy association source lifetime shortened")
            elif binding.get("association_mode"):
                raise ValueError("unknown recovery association mode")
            if (
                content_hash(public) != binding["public_body_sha256"]
                or baseline.get("source_rca_id") != binding["source_rca_id"]
                or baseline.get("engine") != binding["source_engine"]
                or procedure_identity(related_public) != procedure_identity(approved)
            ):
                raise ValueError("canonical body, provenance or complete procedure differs")
            followup = {
                **key,
                "execution_id": row["execution_id"],
                "playbook_digest": row["playbook_digest"],
                "review_sha256": row["review_sha256"],
                "completed_playbook_id": draft["playbook_id"],
                "pending_key": row["pending_key"],
                "execution_binding": {
                    field: execution[field]
                    for field in (
                        "rca_id",
                        "execution_id",
                        "engine",
                        "approval_id",
                        "approved_playbook_s3_key",
                        "playbook_digest",
                        "source_part_revision",
                        "source_part_payload_sha256",
                        "evidence_s3_key",
                        "retrospective_diff_s3_key",
                    )
                },
                "binding_key": binding_key,
                "binding": {k: v for k, v in binding.items() if k not in {"PK", "SK"}},
            }
            merged, diff = merge_playbook_update(related_public, review["update"])
            published = apply_retrospective_verification(approved, merged)
            publication_result = {
                "status": "NO_CHANGE" if diff.is_empty else "UPDATED",
                "summary": review["rationale"][:500],
                "playbook_snapshot_s3_key": row["approved_playbook_s3_key"],
                "diff_s3_key": row["review_s3_key"],
                "followup": followup,
            }
            eid = row["execution_id"]
            # The current completed analysis owns the new publication. Its
            # engine may differ from both approval origin and prior public source.
            publication_engine = parent.get("engine")
            if publication_engine not in {"strands", "headless-codex"}:
                raise ValueError("completed analysis publication engine unavailable")
            work = library.snapshot(public_id, f"retrospective:{eid}")
            head = library.head(public_id)
            if not head or head.get("revision") != revision or head.get("publication_status") != "PUBLISHED":
                raise ValueError("canonical publication changed or incomplete")
            if not work:
                self.execution_store.save_playbook_revision(
                    row["rca_id"],
                    publication_engine,
                    published,
                    execution_id=eid,
                    publication_guard=self._lease_guard(row, token),
                )
            else:
                if (
                    work.get("retrospective_result") != publication_result
                    or work.get("source_rca_id") != row["rca_id"]
                    or work.get("engine") != publication_engine
                    or work.get("revision") != f"retrospective:{eid}"
                    or work.get("baseline_revision") != revision
                    or content_hash(json.loads(work["baseline_playbook_json"])) != content_hash(public)
                    or content_hash(json.loads(work["playbook_json"])) != content_hash(published)
                ):
                    raise ValueError("retained publication work belongs to another binding")
                published = json.loads(work["playbook_json"])
            # A staged snapshot does not prove its vector write succeeded. Retry
            # the same immutable vector before committing/finalizing publication.
            if not self.playbook_store.save_to_s3_vectors(
                published,
                row["rca_id"],
                publication_id=eid,
                baseline_playbook={**public, "library_revision": revision},
                source_engine=publication_engine,
                publication_result=publication_result,
                publication_guard=self._lease_guard(row, token),
            ):
                raise RuntimeError("deferred vector publication incomplete")
            if binding.get("association_mode") == "LEGACY_SAME_GENERATION":
                # Index IO may outlive source changes. Re-read all retained
                # sources and typed edges before committing any public revision.
                fresh_execution = self._get({"PK": row["PK"], "SK": f"EXEC#{row['execution_id']}"})
                fresh_approved, _, fresh_evidence, fresh_review, fresh_expiry = self._sources(fresh_execution or {})
                if (
                    fresh_approved != approved
                    or fresh_evidence != row["evidence_sha256"]
                    or fresh_review != row["review_sha256"]
                    or fresh_expiry != row["body_expires_at"]
                ):
                    raise ValueError("legacy retained sources changed during publication")
                fresh_parent = self._get(binding["legacy_association"]["parent_key"])
                raw_draft = (fresh_parent or {}).get("completion_playbook", (fresh_parent or {}).get("playbook"))
                fresh_draft = (
                    json.loads(raw_draft, object_pairs_hook=_unique_object) if isinstance(raw_draft, str) else {}
                )
                fresh = inspect_association(self, row, fresh_parent or {}, fresh_draft, fresh_approved)
                if (
                    not fresh
                    or any(binding.get(k) != v for k, v in fresh[0].items() if k not in {"created_at", "ttl"})
                    or int(binding["ttl"]) > int(fresh[0]["ttl"])
                ):
                    raise ValueError("legacy association changed during publication")
            committed = self._get({"PK": row["PK"], "SK": f"{publication_engine}#PLAYBOOK_REVISION"})
            if (
                committed
                and committed.get("revised_by_execution_id") == eid
                and content_hash(json.loads(committed["playbook"])) != content_hash(published)
            ):
                raise ValueError("committed publication body differs from retained review")
            if not committed or committed.get("revised_by_execution_id") != eid:
                self.execution_store.publish_playbook_revision(
                    row["rca_id"],
                    publication_engine,
                    published,
                    execution_id=eid,
                    publication_guard=self._lease_guard(row, token),
                )
            if not self.playbook_store.finalize_publication(
                public_id, row["rca_id"], publication_id=eid, followup_token=token
            ):
                raise RuntimeError("deferred canonical finalization incomplete")
        except (ValueError, KeyError, TypeError) as exc:
            self._finish(row, token, "BLOCKED", type(exc).__name__ + ": " + str(exc)[:250])
        except Exception as exc:
            self._finish(row, token, "FAILED", "publication failed: " + type(exc).__name__)

    def _lease_guard(self, row, token):
        """Fence each durable staging/commit and check before and after immutable vector writes."""
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": _pack({"PK": row["PK"], "SK": row["SK"]}),
                "ConditionExpression": "#status = :publishing AND lease_token = :token AND lease_until > :now "
                "AND #ttl > :now AND body_expires_at > :now",
                "ExpressionAttributeNames": {"#status": "status", "#ttl": "ttl"},
                "ExpressionAttributeValues": _pack(
                    {":publishing": "PUBLISHING", ":token": token, ":now": int(self.clock())}
                ),
            }
        }

    def tick(self):
        """Query pending work directly; discover historical reviews separately in bounded scan pages."""
        args = {
            "TableName": self.table,
            "KeyConditionExpression": "PK = :pending",
            "ExpressionAttributeValues": _pack({":pending": "RETROSPECTIVE_PENDING"}),
            "Limit": 1,
            "ConsistentRead": True,
        }
        if self.work_cursor:
            args["ExclusiveStartKey"] = self.work_cursor
        result = self.ddb.query(**args)
        self.work_cursor = result.get("LastEvaluatedKey")
        items = result.get("Items", [])
        if items:
            work = _unpack(items[0])
            child = self._get(work["followup_key"])
            if child and child.get("status") in {"PUBLISHED", "BLOCKED"}:
                self.ddb.transact_write_items(
                    TransactItems=[
                        {
                            "ConditionCheck": {
                                "TableName": self.table,
                                "Key": _pack(work["followup_key"]),
                                "ConditionExpression": "#status = :status",
                                "ExpressionAttributeNames": {"#status": "status"},
                                "ExpressionAttributeValues": _pack({":status": child["status"]}),
                            }
                        },
                        {
                            "Delete": {
                                "TableName": self.table,
                                "Key": _pack({"PK": work["PK"], "SK": work["SK"]}),
                                "ConditionExpression": "followup_key = :key",
                                "ExpressionAttributeValues": _pack({":key": work["followup_key"]}),
                            }
                        },
                    ]
                )
            elif child:
                self.resume(child)
        self.discovery_turn += 1
        if items and self.discovery_turn % 5:
            return
        if not self.pending:
            args = {"TableName": self.table, "Limit": 25}
            if self.cursor:
                args["ExclusiveStartKey"] = self.cursor
            result = self.ddb.scan(**args)
            self.cursor = result.get("LastEvaluatedKey")
            self.pending.extend(_unpack(item) for item in result.get("Items", []))
        while self.pending:
            row = self.pending.popleft()
            if (
                row.get("SK", "").startswith("EXEC#")
                and row.get("source_part") == "recovery"
                and row.get("execution_state") == "RESOLVED"
                and row.get("retrospective_status") in {"FAILED", "COMPLETED"}
                and row.get("retrospective_diff_s3_key")
            ):
                child = self.enroll(row["rca_id"], row["execution_id"])
                if child:
                    self.resume(child)
                return
