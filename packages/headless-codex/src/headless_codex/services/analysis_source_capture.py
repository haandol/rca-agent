"""Archive actual readonly MCP receipts, separating incident-time sources from later observations."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import unquote

_SHA = re.compile(r"[a-fA-F0-9]{40}")


def _timestamp(value):
    """Accept explicit instants only; absent dates cannot establish an incident-time source."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _objects(value):
    """Unwrap only actual MCP JSON payloads, not arbitrary nested source text or commit messages."""
    if not isinstance(value, dict):
        return
    candidates = []
    structured = value.get("structured_content", value.get("structuredContent"))
    if isinstance(structured, dict):
        candidates.append(structured)
    for block in value.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                candidates.append(json.loads(block.get("text", "")))
            except (ValueError, TypeError):
                continue
    for candidate in candidates:
        if isinstance(candidate, dict):
            yield candidate
        elif isinstance(candidate, list):
            yield from (item for item in candidate if isinstance(item, dict))


def _file_objects(result: dict, arguments: dict):
    """Recognize actual GitHub MCP embedded text resources as well as Contents API JSON replies."""
    blocks = result.get("content", [])
    download = isinstance(blocks, list) and (
        any(
            isinstance(block, dict)
            and isinstance(block.get("text"), str)
            and block["text"].startswith("successfully downloaded")
            for block in blocks
        )
        or (
            len(blocks) >= 2
            and all(isinstance(block, dict) and isinstance(block.get("text"), str) for block in blocks)
        )
    )
    if not download:
        yield from _objects(result)
        return
    # A downloaded JSON file is opaque source, not another Contents API envelope.
    if len(blocks) != 2 or not all(isinstance(block, dict) for block in blocks):
        return
    owner, repo = arguments.get("owner"), arguments.get("repo")
    revision = arguments.get("sha") or arguments.get("ref")
    path = arguments.get("path", "").lstrip("/")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        return
    blocks = result.get("content")
    if (
        isinstance(blocks, list)
        and len(blocks) == 2
        and all(
            isinstance(block, dict) and block.get("type", "text") == "text" and isinstance(block.get("text"), str)
            for block in blocks
        )
    ):
        match = re.fullmatch(r"successfully downloaded (?:text|empty) file \(SHA: ([a-f0-9]{40})\)", blocks[0]["text"])
        if match:
            yield {
                "type": "file",
                "path": path,
                "sha": match.group(1),
                "encoding": "text",
                "content": blocks[1]["text"],
            }
        return
    expected_uri = f"repo://{owner}/{repo}/sha/{revision}/contents/{path}"
    summaries = [block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"]
    hashes = [
        match.group(1)
        for text in summaries
        if (match := re.fullmatch(r"successfully downloaded (?:text|empty) file \(SHA: ([a-f0-9]{40})\)", text))
    ]
    if len(hashes) != 1:
        return
    for block in result.get("content", []):
        resource = block.get("resource")
        if (
            block.get("type") == "resource"
            and isinstance(resource, dict)
            and isinstance(resource.get("uri"), str)
            and unquote(resource["uri"]) == expected_uri
            and isinstance(resource.get("text"), str)
        ):
            yield {"type": "file", "path": path, "sha": hashes[0], "encoding": "text", "content": resource["text"]}


def _mapped_manifest_source(value: dict, path: str, digest: str, revision: str, repository: str) -> bool:
    """Treat a declared Git location as a locator only; the actual read must match installed file hashes."""
    files, locations = value.get("files"), value.get("source_locations")
    if not isinstance(files, dict) or not isinstance(locations, dict):
        return False
    if value.get("fingerprint") != hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest():
        return False
    matches = [
        (installed_path, location)
        for installed_path, location in locations.items()
        if isinstance(location, dict) and location.get("path") == path
    ]
    if len(matches) != 1:
        return False
    installed_path, location = matches[0]
    return (
        isinstance(installed_path, str)
        and not installed_path.startswith("/")
        and all(segment not in {"", ".", ".."} for segment in installed_path.split("/"))
        and files.get(installed_path) == digest
        and location.get("sha256") == digest
        and location.get("repository") == repository
        and location.get("commit") == revision
        and location.get("verification") == "declared"
    )


def _incident_source_phase(incident: dict, path: str, digest: str, revision: str, repository: str = "") -> str | None:
    """Retain source phase so a correct normal baseline cannot become the faulty deployment's code."""
    for source in incident.get("source_artifacts", []):
        if source.get("path") == path and source.get("sha256") == digest:
            phase = source.get("source_phase", source.get("phase", "supplied_snapshot"))
            return "normal_baseline" if phase in {"normal", "baseline", "normal_baseline"} else phase

    def inspect(value):
        """Find content/revision witnesses only inside a server-frozen source phase."""
        if isinstance(value, dict):
            if (
                value.get("event") == "source_manifest"
                and value.get("verified") is True
                and isinstance(value.get("files"), dict)
                and (
                    value["files"].get(path) == digest
                    or _mapped_manifest_source(value, path, digest, revision, repository)
                )
            ):
                return True
            if any(
                revision and value.get(key) == revision
                for key in ("source_revision", "commit_sha", "commitId", "git_sha")
            ):
                return True
            return any(inspect(item) for item in value.values())
        return any(inspect(item) for item in value) if isinstance(value, list) else False

    observations = incident.get("observations", {})
    if inspect(observations.get("current", {})) or inspect(observations.get("critical_facts", [])):
        return "incident"
    if inspect(observations.get("baseline", {})):
        return "normal_baseline"
    if inspect(
        {key: value for key, value in observations.items() if key not in {"baseline", "current", "recovery_evidence"}}
    ):
        return "supplied_snapshot"
    return None


def capture_sources(raw_output: str, incident: dict) -> dict:
    """Use completed GitHub receipts at verified pre-cutoff revisions; preserve a separate read-time ledger.

    Unsupported response forms remain unavailable. Tool argument claims alone do not
    establish source contents, a Git commit date, or that current state was incident state.
    """
    alarm = incident.get("alarm", {})
    cutoff = _timestamp(alarm.get("StateChangeTime") or alarm.get("state_change_time"))
    receipts = []
    for line in raw_output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
            receipts.append(item)
    commits = {}
    ledger = []
    for receipt in receipts:
        args = receipt.get("arguments", {})
        result = receipt.get("result")
        if not isinstance(args, dict) or not isinstance(result, dict) or receipt.get("error") or result.get("isError"):
            continue
        tool, server = str(receipt.get("tool", "")), str(receipt.get("server", ""))
        ended = _timestamp(args.get("end_time") or args.get("endTime") or args.get("EndTime"))
        began = _timestamp(args.get("start_time") or args.get("startTime") or args.get("StartTime"))
        bounded = bool(cutoff and ended and began and began <= ended <= cutoff)
        ledger.append(
            {
                "ref": f"mcp:{receipt.get('id', '')}",
                "server": server,
                "tool": tool,
                "window": "incident" if bounded else "later_or_unbounded",
                "start": began.isoformat() if began else None,
                "end": ended.isoformat() if ended else None,
            }
        )
        if tool == "get_commit" and cutoff:
            for obj in _objects(result):
                commit = obj.get("commit")
                if not isinstance(commit, dict):
                    continue
                date = _timestamp(commit.get("committer", {}).get("date"))
                sha = obj.get("sha", "")
                if date and date <= cutoff and isinstance(sha, str) and _SHA.fullmatch(sha):
                    commits[(args.get("owner"), args.get("repo"), sha)] = date.isoformat()
    sources = []
    for receipt in receipts:
        args, result = receipt.get("arguments", {}), receipt.get("result")
        if receipt.get("tool") != "get_file_contents" or not isinstance(args, dict) or not isinstance(result, dict):
            continue
        if receipt.get("error") or result.get("isError"):
            continue
        owner, repo, revision = args.get("owner"), args.get("repo"), args.get("sha") or args.get("ref")
        date = commits.get((owner, repo, revision))
        if not date:
            continue
        for obj in _file_objects(result, args):
            if obj.get("type") != "file" or obj.get("path") != str(args.get("path", "")).lstrip("/"):
                continue
            content = obj.get("content")
            if not isinstance(content, str) or obj.get("truncated"):
                continue
            try:
                if obj.get("encoding") == "base64":
                    raw = base64.b64decode("".join(content.split()), validate=True)
                    text = raw.decode("utf-8")
                elif obj.get("encoding") in {"utf-8", "utf8", "text"}:
                    text, raw = content, content.encode()
                else:
                    continue
            except (ValueError, UnicodeError):
                continue
            # GitHub's blob digest binds the body, independently of model prose and path claims.
            blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            if obj.get("sha") != blob:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            phase = _incident_source_phase(incident, obj["path"], digest, revision, f"{owner}/{repo}")
            if phase is None:
                continue
            source = {
                "source_phase": phase,
                "repository": f"{owner}/{repo}",
                "base_revision": revision,
                "path": obj["path"],
                "text": text,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source_ref": f"github://{owner}/{repo}@{revision}/{obj['path']}",
                "revision_kind": "git_commit",
                "committed_at": date,
            }
            if source not in sources:
                sources.append(source)
    return {"sources": sources, "queries": ledger}
