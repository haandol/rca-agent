"""Read only server-bound documents for the current retrospective, with explicit paging."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.config import Config

from headless_codex.config.settings import AWS_REGION, S3_EVIDENCE_BUCKET
from headless_codex.services.execution_evidence import _redact_json, redact
from headless_codex.services.execution_workspace import workspace_for_token, write_observation_json

_REFERENCE = "retrospective-reference.json"
_READ_STATE = "retrospective-read-state.json"
_DOCUMENTS = {"evidence", "approved_playbook"}
_MAX_REPLY = 12000


class SelectorError(ValueError):
    """The source is intact; the caller may correct the pointer or page arguments."""


@lru_cache(maxsize=2)
def _model_view(raw: bytes) -> dict:
    """Apply the existing structured policy to whole strings before selecting or chunking.

    The exact private bytes remain the cache/integrity authority. At most two derived
    views are retained in this MCP process, avoiding re-redaction on every small page.
    """
    return _redact_json(json.loads(raw))


def _bounded_reply(reply: dict) -> dict:
    """Every public branch, including scalars and errors, has the same serialized limit."""
    if len(json.dumps(reply, ensure_ascii=False, allow_nan=False)) > _MAX_REPLY:
        raise SelectorError("selected value or metadata exceeds reply budget; use a narrower pointer")
    return reply


def _safe_pointer(pointer: str) -> None:
    """Do not leak credentials embedded in source keys or silently rename their references."""
    segments = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")]
    if redact(pointer) != pointer or any(redact(part) != part for part in segments):
        raise SelectorError("source pointer contains sensitive data and cannot be exposed")


def _matching_playbooks(token: str) -> None:
    paths = [_path(token, f"retrospective-{doc}-cache.json") for doc in ("evidence", "approved_playbook")]
    if not all(path.exists() for path in paths):
        return
    evidence, approved = [json.loads(path.read_bytes()) for path in paths]
    identifier = evidence.get("playbook_id")
    if not isinstance(identifier, str) or not identifier or identifier != approved.get("playbook_id"):
        raise ValueError("evidence and approved snapshot playbook identity differs")


def _path(token: str, name: str) -> Path:
    """Only fixed filenames under the token-owned directory are accessible."""
    root = workspace_for_token(token)
    path = root / name
    if not root.is_dir() or root.is_symlink() or path.is_symlink():
        raise ValueError("retrospective workspace is unavailable or unsafe")
    return path


@contextmanager
def _token_lock(token: str):
    """Serialize a token's readers and validators across MCP threads and processes."""
    path = _path(token, "retrospective-read.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # Closing also releases the lock if the caller raised.


def write_reference(
    token: str,
    *,
    rca_id: str,
    execution_id: str,
    evidence_key: str,
    approved_playbook_key: str,
    playbook_digest: str,
    bucket: str,
) -> dict:
    """Publish locations only after the orchestrator has persisted the resolved evidence to S3."""
    ref = {
        "rca_id": rca_id,
        "execution_id": execution_id,
        "bucket": bucket,
        "documents": {"evidence": evidence_key, "approved_playbook": approved_playbook_key},
        "playbook_digest": playbook_digest,
    }
    _validate_reference(ref, execution_id, bucket)
    with _token_lock(token):
        path = _path(token, _REFERENCE)
        if path.exists():
            raise ValueError("retrospective reference is already fixed")
        write_observation_json(path, ref)
    return ref


def _validate_reference(ref: dict, execution_id: str, bucket: str) -> None:
    """Reject cross-execution keys before S3; the model never supplies any of these coordinates."""
    rca = ref.get("rca_id")
    if (
        not isinstance(rca, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", rca)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", execution_id)
        or ref.get("execution_id") != execution_id
        or not bucket
        or ref.get("bucket") != bucket
        or ref.get("documents")
        != {
            "evidence": f"executions/{rca}/{execution_id}/evidence.json",
            "approved_playbook": f"approvals/{rca}/{execution_id}/playbook.json",
        }
        or not isinstance(ref.get("playbook_digest"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", ref["playbook_digest"])
    ):
        raise ValueError("retrospective reference does not match current execution")


def _reference(token: str, execution_id: str) -> tuple[dict, str]:
    """Load the server manifest and bind read receipts to its exact bytes."""
    raw = _path(token, _REFERENCE).read_bytes()
    ref = json.loads(raw)
    _validate_reference(ref, execution_id, S3_EVIDENCE_BUCKET)
    return ref, hashlib.sha256(raw).hexdigest()


def _s3_client():
    """Use the existing role/provider chain; no supplied credentials, profile, endpoint or bucket."""
    return boto3.client(
        "s3",
        region_name=AWS_REGION,
        config=Config(connect_timeout=5, read_timeout=60, retries={"total_max_attempts": 1, "mode": "standard"}),
    )


def _document(token: str, ref: dict, document: str) -> tuple[dict, str]:
    """Fetch the exact durable object once and verify identity/digest before private caching."""
    cache = _path(token, f"retrospective-{document}-cache.json")
    if cache.exists():
        raw = cache.read_bytes()
    else:
        response = _s3_client().get_object(Bucket=ref["bucket"], Key=ref["documents"][document])
        body = response["Body"]
        try:
            raw = body.read()
        finally:
            body.close()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("retrospective source is not a JSON object")
    if document == "evidence":
        if (
            value.get("execution_id") != ref["execution_id"]
            or value.get("rca_id") != ref["rca_id"]
            or value.get("final_state") != "RESOLVED"
            or value.get("resolution_confirmed") is not True
        ):
            raise ValueError("retrospective evidence identity or resolved state differs")
    elif hashlib.sha256(raw).hexdigest() != ref["playbook_digest"]:
        raise ValueError("approved playbook digest differs")
    digest = hashlib.sha256(raw).hexdigest()
    if not cache.exists():
        # Publish complete exact bytes atomically, without replacing any existing cache.
        fd, temporary = tempfile.mkstemp(prefix=f".{cache.name}.", dir=cache.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, cache)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return _model_view(raw), digest


def _pointer(value, pointer: str):
    """Resolve JSON pointers inside the fixed document, not filesystem or S3 paths."""
    if pointer == "":
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise SelectorError("invalid JSON pointer")
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise SelectorError("array pointer requires an index")
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise SelectorError("pointer traverses a scalar")
    return value


def _child(pointer: str, key) -> str:
    """Escape source keys so returned pointers can be reused without interpretation."""
    if redact(str(key)) != str(key):
        raise SelectorError("source pointer contains sensitive data and cannot be exposed")
    child = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
    _safe_pointer(child)
    return child


def _describe(value, pointer: str) -> dict:
    """Index nested values without inserting their full contents into a tool reply."""
    if isinstance(value, dict):
        labels = {
            k: v
            for k, v in value.items()
            if k
            in {
                "step_id",
                "attempt_index",
                "type",
                "phase",
                "role",
                "observed_at",
                "recorded_at",
                "started_at",
                "ended_at",
                "status",
                "succeeded",
                "blocked",
                "failure_class",
                "resolved",
                "criteria_met",
                "manual_action_required",
            }
            and (v is None or isinstance(v, (bool, int, float)) or isinstance(v, str) and len(v) <= 160)
        }
        return {"pointer": pointer, "kind": "object", "field_count": len(value), "labels": labels, "index_only": True}
    if isinstance(value, list):
        return {"pointer": pointer, "kind": "array", "item_count": len(value), "index_only": True}
    if isinstance(value, str) and len(value) > 160:
        return {"pointer": pointer, "kind": "string", "total_chars": len(value), "index_only": True}
    return {"pointer": pointer, "kind": "scalar", "value": value, "complete": True}


def _page(value, pointer: str, offset: int, max_items: int, text_chars: int, source: dict) -> dict:
    """Every omitted part has a count/pointer/cursor; never silently slice a JSON document."""
    if type(offset) is not int or offset < 0 or type(max_items) is not int or not 1 <= max_items <= 50:
        raise SelectorError("invalid page offset or item limit")
    if type(text_chars) is not int or not 1 <= text_chars <= 4000:
        raise SelectorError("invalid text page size")
    reply = {"ok": True, "source": source, "pointer": pointer, "offset": offset}
    _safe_pointer(pointer)
    _bounded_reply(reply)
    if isinstance(value, str):
        if offset > len(value):
            raise SelectorError("text offset exceeds source length")
        length = min(text_chars, len(value) - offset)
        while True:
            end = offset + length
            reply.update(
                kind="string",
                text=value[offset:end],
                total_chars=len(value),
                complete=end == len(value),
                next_offset=end if end < len(value) else None,
            )
            if len(json.dumps(reply, ensure_ascii=False)) <= _MAX_REPLY:
                return _bounded_reply(reply)
            if not length:
                raise SelectorError("source metadata exceeds reply budget")
            length //= 2
    if isinstance(value, (dict, list)):
        keys = list(value) if isinstance(value, dict) else list(range(len(value)))
        if offset > len(keys):
            raise SelectorError("page offset exceeds source length")
        reply.update(kind="object" if isinstance(value, dict) else "array", total_items=len(keys), items=[])
        end = offset
        for key in keys[offset : offset + max_items]:
            item = {"key": key, **_describe(value[key], _child(pointer, key))}
            candidate = {**reply, "items": [*reply["items"], item], "next_offset": end + 1, "complete": False}
            if len(json.dumps(candidate, ensure_ascii=False)) > _MAX_REPLY:
                if not reply["items"]:
                    raise SelectorError("item metadata exceeds reply budget; use a narrower pointer")
                break
            reply["items"].append(item)
            end += 1
        reply.update(complete=end == len(keys), next_offset=end if end < len(keys) else None)
        return _bounded_reply(reply)
    if offset:
        raise SelectorError("scalar offset must be zero")
    reply.update(kind="scalar", value=value, complete=True, next_offset=None)
    return _bounded_reply(reply)


def _state(token: str, fingerprint: str) -> dict:
    """A different manifest cannot reuse another read session's attestation prerequisites."""
    path = _path(token, _READ_STATE)
    if not path.exists():
        return {"reference_sha256": fingerprint, "documents_read": [], "failed": False}
    state = json.loads(path.read_text())
    if state.get("reference_sha256") != fingerprint:
        raise ValueError("retrospective read session reference changed")
    return state


def read_document(
    token: str,
    execution_id: str,
    document: str,
    pointer: str = "",
    *,
    offset: int = 0,
    max_items: int = 20,
    text_chars: int = 4000,
) -> dict:
    """Keep cache publication, read-state mutation and error recording under one token lock."""
    try:
        with _token_lock(token):
            return _read_document_locked(
                token,
                execution_id,
                document,
                pointer,
                offset=offset,
                max_items=max_items,
                text_chars=text_chars,
            )
    except Exception:
        return {
            "ok": False,
            "error": "retrospective source read failed",
            "detail": "source synchronization unavailable",
            "recoverable": False,
        }


def _read_document_locked(
    token: str,
    execution_id: str,
    document: str,
    pointer: str = "",
    *,
    offset: int = 0,
    max_items: int = 20,
    text_chars: int = 4000,
) -> dict:
    """Read only a fixed current-execution document and record successes/failures for attestation."""
    fingerprint = ""
    try:
        ref, fingerprint = _reference(token, execution_id)
        if document not in _DOCUMENTS:
            raise SelectorError("unknown retrospective document")
        value, digest = _document(token, ref, document)
        state = _state(token, fingerprint)
        if state.get("failed"):
            raise ValueError("retrospective source read previously failed")
        previous = state.get("document_sha256", {}).get(document)
        if previous is not None and previous != digest:
            raise ValueError("retrospective cached source changed")
        _matching_playbooks(token)
        try:
            selected = _pointer(value, pointer)
        except (KeyError, IndexError) as exc:
            raise SelectorError("JSON pointer does not exist") from exc
        result = _page(
            selected,
            pointer,
            offset,
            max_items,
            text_chars,
            {
                "document": document,
                "key": ref["documents"][document],
                "sha256": digest,
                "sha256_scope": "original_stored_bytes",
                "view": "redacted_stored_json",
                "offset_scope": "redacted_value",
                "rca_id": ref["rca_id"],
                "execution_id": execution_id,
            },
        )
        if document not in state["documents_read"]:
            state["documents_read"].append(document)
        state.setdefault("document_sha256", {})[document] = digest
        write_observation_json(_path(token, _READ_STATE), state)
        return result
    except SelectorError as exc:
        return _bounded_reply(
            {
                "ok": False,
                "error": "invalid retrospective selector",
                "detail": redact(str(exc))[:500],
                "recoverable": True,
            }
        )
    except Exception as exc:
        try:
            state = _state(token, fingerprint)
            state.update(failed=True, error_type=type(exc).__name__)
            write_observation_json(_path(token, _READ_STATE), state)
        except Exception:
            pass
        return _bounded_reply(
            {
                "ok": False,
                "error": "retrospective source read failed",
                "detail": redact(str(exc))[:500]
                if isinstance(exc, (ValueError, KeyError, IndexError))
                else "source unavailable",
                "recoverable": False,
            }
        )


def require_successful_reads(token: str, execution_id: str) -> None:
    """No read, missing data or a failed read cannot authorize a persisted retrospective draft."""
    with _token_lock(token):
        _require_successful_reads_locked(token, execution_id)


def _require_successful_reads_locked(token: str, execution_id: str) -> None:
    _, fingerprint = _reference(token, execution_id)
    state = _state(token, fingerprint)
    if state.get("failed") or set(state.get("documents_read", [])) != _DOCUMENTS:
        raise ValueError("retrospective requires successful reads of evidence and approved playbook")
    for document in _DOCUMENTS:
        raw = _path(token, f"retrospective-{document}-cache.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != state.get("document_sha256", {}).get(document):
            raise ValueError("retrospective cached source changed after reading")
    _matching_playbooks(token)
