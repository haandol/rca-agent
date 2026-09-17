"""Retain exact internal RCA/Report files for redelivery without creating a fourth logical part."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from headless_codex.services.analysis_part_workspace import fixed_path
from headless_codex.services.analysis_parts import json_bytes

_ALLOWED = re.compile(
    r"(?:scoping\.json|hypotheses(?:-[23])?\.json|validation-[1-3]\.json|report\.md|playbook\.json|code-proposal\.json|root-source-artifacts\.json)"
)


def archive_root_files(store, rca_id: str, token: str) -> dict:
    """Save content-addressed internal files before the root result so a retry need not rerun analysis."""
    base = fixed_path(token, "report.md").parent
    artifacts = {
        p.name: p.read_text()
        for p in base.iterdir()
        if p.is_file() and not p.is_symlink() and _ALLOWED.fullmatch(p.name)
    }
    payload = {
        "schema_version": 1,
        "rca_id": rca_id,
        "engine": store.engine,
        "kind": "root_internal_artifacts",
        "artifacts": artifacts,
    }
    raw = json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    key = f"analysis-parts/{store.engine}/{rca_id}/root_cause/{digest}.json"
    store._put_object(key, raw)
    return {"kind": "root_internal_artifacts", "key": key, "sha256": digest}


def restore_root_files(store, rca_id: str, token: str, reference: dict) -> None:
    """Rehydrate only a verified server-created bundle; never infer files from a report or latest library."""
    digest = reference.get("sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid root archive hash")
    key = f"analysis-parts/{store.engine}/{rca_id}/root_cause/{digest}.json"
    if reference.get("key") != key or reference.get("kind") != "root_internal_artifacts":
        raise ValueError("root archive scope mismatch")
    raw = store._read_object(key)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("root archive bytes differ")
    payload = json.loads(raw)
    if (
        payload.get("rca_id") != rca_id
        or payload.get("engine") != store.engine
        or payload.get("schema_version") != 1
        or payload.get("kind") != "root_internal_artifacts"
        or not isinstance(payload.get("artifacts"), dict)
    ):
        raise ValueError("root archive identity differs")
    for name, text in payload["artifacts"].items():
        if not _ALLOWED.fullmatch(name) or not isinstance(text, str):
            raise ValueError("unsupported root archive file")
        path = fixed_path(token, name)
        encoded = text.encode()
        if path.exists():
            if path.read_bytes() != encoded:
                raise ValueError("root artifact differs from immutable archive")
            continue
        fd, temporary = tempfile.mkstemp(prefix=".root-restore-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def archive_operations_sources(store, rca_id: str, token: str) -> dict | None:
    """Persist current CI read receipts separately, so later review never rewrites incident-time evidence."""
    base = fixed_path(token, "analysis-parts-context.json").parent
    sources = []
    for path in sorted(base.glob("operations-source-*.json")):
        if path.is_symlink():
            raise ValueError("unsafe operations source file")
        sources.append(json.loads(path.read_bytes()))
    if not sources:
        return None
    payload = {
        "schema_version": 1,
        "rca_id": rca_id,
        "engine": store.engine,
        "kind": "current_ci_sources",
        "sources": sources,
    }
    raw = json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    key = f"analysis-parts/{store.engine}/{rca_id}/operations/{digest}.json"
    store._put_object(key, raw)
    return {"kind": "current_ci_sources", "key": key, "sha256": digest}


def read_operations_sources(store, rca_id: str, reference: dict) -> list[dict]:
    """Bind visible CI bodies to this exact immutable part archive, never a current repository read."""
    digest = reference.get("sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid operations archive hash")
    key = f"analysis-parts/{store.engine}/{rca_id}/operations/{digest}.json"
    if reference.get("kind") != "current_ci_sources" or reference.get("key") != key:
        raise ValueError("operations archive scope mismatch")
    raw = store._read_object(key)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("operations archive bytes differ")
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("rca_id") != rca_id
        or payload.get("engine") != store.engine
        or payload.get("kind") != "current_ci_sources"
        or not isinstance(payload.get("sources"), list)
    ):
        raise ValueError("operations archive identity differs")
    artifacts = []
    for source in payload["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("text"), str):
            raise ValueError("operations source body is unavailable")
        content = source["text"].encode("utf-8")
        repository, path, blob = (source.get(name) for name in ("repository", "path", "blob_sha"))
        if (
            source.get("kind") != "current_ci"
            or source.get("revision_kind") != "git_blob_snapshot"
            or not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository)
            or not isinstance(path, str)
            or path.startswith("/")
            or any(segment in {"", ".", ".."} for segment in path.split("/"))
            or source.get("sha256") != hashlib.sha256(content).hexdigest()
            or blob != hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            or source.get("source_ref") != f"github://{repository}@{blob}/{path}"
        ):
            raise ValueError("operations source content or identity differs")
        artifacts.append(
            {
                **source,
                "source_kind": "read_control_configuration",
                "source_phase": "current_ci",
                # This is a Git blob identity, not a verified commit or current branch head.
                "base_revision": f"blob:{blob}",
            }
        )
    return artifacts
