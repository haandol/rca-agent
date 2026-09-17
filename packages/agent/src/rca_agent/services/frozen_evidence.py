"""Bound post-publication reads to historical incident evidence and immutable source revisions."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

from rca_agent.services.collected_observations import _objects

_SCOPE = ContextVar("frozen_incident_evidence", default=None)


def _time(value) -> float:
    """Accept recorded aware timestamps or epoch values without reinterpreting them as current time."""
    if isinstance(value, (float, int)):
        return value / 1000 if value > 10**11 else float(value)
    if not isinstance(value, str):
        raise ValueError("historical query timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("historical query timestamp must be timezone aware")
    return parsed.timestamp()


@contextmanager
def frozen_evidence_scope(scoping):
    """Attach immutable source boundaries to new providers without enabling model-eval's supplied-only mode."""
    token = _SCOPE.set(scoping.model_copy(deep=True))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_scope():
    """Capture the incident scope when a per-invocation tool owner is created."""
    return _SCOPE.get()


def bound_request(
    scoping, tool: str, arguments: dict, query_ids: set[str], *, input_schema: dict | None = None
) -> dict:
    """Bound historical query end times without narrowing legitimate readonly discovery or older history."""
    if scoping is None:
        return arguments
    args = deepcopy(arguments)
    if tool == "get_file_contents":
        if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", str(args.get("ref", ""))):
            raise ValueError("source content requires an immutable commit reference, not a branch or latest")
        return args
    observations = scoping.incident_observations
    current, baseline = observations.current, observations.baseline
    window = current.get("log_window", {})
    cutoff = (
        getattr(scoping, "_frozen_cutoff", None)
        or window.get("end")
        or window.get("end_time")
        or (
            scoping.raw_alarm.state_change_time.isoformat()
            if scoping.raw_alarm and scoping.raw_alarm.state_change_time
            else None
        )
    )
    starts = [key for key in args if key.lower().replace("_", "") in {"starttime", "startdatetime"}]
    ends = [key for key in args if key.lower().replace("_", "") in {"endtime", "enddatetime"}]
    if cutoff and not ends:
        properties = (input_schema or {}).get("properties", {})
        supported = [key for key in properties if key.lower().replace("_", "") in {"endtime", "enddatetime"}]
        if supported:
            key = supported[0]
            shape = properties[key]
            args[key] = (
                int(_time(cutoff) * (1000 if tool in {"get_log_events", "filter_log_events"} else 1))
                if shape.get("type") in {"integer", "number"}
                else cutoff
            )
            ends = [key]
    if cutoff:
        for key in ends:
            if _time(args[key]) > _time(cutoff):
                raise ValueError(f"query extends beyond the frozen incident cutoff {cutoff}")
        if starts and ends and not _time(args[starts[0]]) < _time(args[ends[0]]):
            raise ValueError("historical query start must precede end")
    # A discovered related metric or resource is not limited to the recovery alarm's coordinates.
    # When the service log group is requested, include both known normal and fault task streams.
    if tool == "execute_log_insights_query":
        group = baseline.get("scope", {}).get("log_group")
        requested = args.get("log_group_names", args.get("logGroupNames"))
        streams = {
            row.get("log_stream")
            for section in (baseline, current)
            for row in section.get("observations", [])
            if row.get("log_stream")
        }
        if group and requested == [group] and streams:
            key = "query_string" if "query_string" in args else "query"
            query = args.get(key)
            if isinstance(query, str) and not re.search(r"\bSOURCE\b", query, re.I):
                args[key] = "filter @logStream in " + json.dumps(sorted(streams)) + " | " + query
    return args


def observation_scope_note(scoping, tool: str, arguments: dict) -> dict | None:
    """Label source timing so current discovery can guide reads without becoming historical incident proof."""
    if scoping is None:
        return None
    ends = [key for key in arguments if key.lower().replace("_", "") in {"endtime", "enddatetime"}]
    cutoff = getattr(scoping, "_frozen_cutoff", None)
    return {
        "server_evidence_scope": {
            "kind": "bounded_historical_query"
            if ends
            else "immutable_source_read"
            if tool == "get_file_contents"
            else "readonly_discovery_not_incident_state",
            "frozen_cutoff": cutoff,
            "query_end": arguments[ends[0]] if ends else None,
            "rule": (
                "Additional reads do not replace the frozen incident. Discovery is not evidence of pre-incident state."
            ),
        }
    }


def _received_files(receipts: list[dict]) -> list[dict]:
    """Decode actual successful immutable repository reads independently of their later use."""
    artifacts = []
    target_refs = set()
    for receipt in receipts:
        args, result = receipt.get("arguments", {}), receipt.get("result", {})
        requested = str(args.get("sha", args.get("ref", "")))
        if (
            receipt.get("tool_name") == "get_commit"
            and receipt.get("request_terminated") is True
            and result.get("status") == "success"
            and not result.get("isError")
            and requested
            and not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", requested)
        ):
            for item in _objects(result):
                if isinstance(item.get("commit"), dict) and re.fullmatch(
                    r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", str(item.get("sha", ""))
                ):
                    target_refs.add((args.get("owner"), args.get("repo"), item["sha"]))
    for receipt in receipts:
        args, result = receipt.get("arguments", {}), receipt.get("result", {})
        if (
            receipt.get("tool_name") != "get_file_contents"
            or receipt.get("request_terminated") is not True
            or result.get("status") != "success"
            or result.get("isError")
            or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", str(args.get("ref", "")))
        ):
            continue
        for document in _objects(result):
            if isinstance(document.get("uri"), str) and isinstance(document.get("text"), str):
                uri = urlparse(document["uri"])
                path = unquote(uri.path)
                prefix = "/" + str(args.get("repo", "")) + "/"
                query_refs = parse_qs(uri.query, keep_blank_values=True).get("ref", [])
                contents = "contents/" + str(args.get("path", ""))
                allowed_paths = {
                    prefix + contents,
                    prefix + args["ref"] + "/" + contents,
                    prefix + "sha/" + args["ref"] + "/" + contents,
                }
                if (
                    uri.scheme != "repo"
                    or uri.netloc != args.get("owner")
                    or path not in allowed_paths
                    or any(ref != args["ref"] for ref in query_refs)
                ):
                    continue
                document = {"path": args["path"], "content": document["text"], "encoding": "utf-8"}
            if document.get("path") != args.get("path") or not isinstance(document.get("content"), str):
                continue
            try:
                raw = (
                    base64.b64decode(document["content"].replace("\n", ""), validate=True)
                    if document.get("encoding") == "base64"
                    else document["content"].encode()
                    if document.get("encoding") in {"utf-8", "utf8"}
                    else None
                )
                if raw is None:
                    continue
                text = raw.decode("utf-8")
            except (ValueError, UnicodeError):
                continue
            blob = document.get("sha")
            if blob and blob != hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest():
                continue
            if not args.get("owner") or not args.get("repo") or not receipt.get("source_ref"):
                continue
            artifact = {
                "path": args["path"],
                "text": text,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source_ref": receipt["source_ref"],
                "base_revision": args["ref"],
                "base_ref": args["ref"],
                "is_current_target": (args["owner"], args["repo"], args["ref"]) in target_refs,
                "identity_kind": "git_commit",
                "repository": args["owner"] + "/" + args["repo"],
                "observed_at": receipt.get("received_at"),
            }
            if artifact not in artifacts:
                artifacts.append(artifact)
    return artifacts


def received_source_artifacts(receipts: list[dict], scoping) -> list[dict]:
    """Only file bytes matching an observed deployed manifest may ground an incident code fix."""
    manifests = []
    for phase in ("baseline", "current"):
        for row in getattr(scoping.incident_observations, phase).get("observations", []):
            message = row.get("message", {})
            if message.get("event") == "source_manifest" and message.get("verified") is True:
                files = message.get("files", {})
                if files and hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() == message.get(
                    "fingerprint"
                ):
                    manifests.append((phase, files, message.get("fingerprint")))
    artifacts = []
    for artifact in _received_files(receipts):
        matches = [
            {"phase": phase, "fingerprint": fingerprint, "path": path}
            for phase, files, fingerprint in manifests
            for path, digest in files.items()
            if (artifact["path"] == path or artifact["path"].endswith("/" + path)) and artifact["sha256"] == digest
        ]
        if matches:
            artifacts.append(
                {
                    **artifact,
                    "observed_manifests": matches,
                    "source_phase": "current" if any(match["phase"] == "current" for match in matches) else "baseline",
                }
            )
    return artifacts


def received_control_artifacts(receipts: list[dict]) -> list[dict]:
    """Keep actually read CI/config separate from deployment proof; absence of a file is not a finding."""
    controls = []
    for artifact in _received_files(receipts):
        path = artifact["path"]
        if path.startswith((".github/workflows/", ".circleci/")) or path.rsplit("/", 1)[-1] in {
            ".gitlab-ci.yml",
            "Jenkinsfile",
            "azure-pipelines.yml",
            "buildspec.yml",
            "pyproject.toml",
            "package.json",
            "tox.ini",
            "Makefile",
            "Dockerfile",
            "build_revision.py",
        }:
            controls.append({**artifact, "source_kind": "read_control_configuration"})
    return controls
