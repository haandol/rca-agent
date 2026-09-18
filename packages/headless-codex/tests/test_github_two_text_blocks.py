"""Actual file response replay inside a synthetic Codex envelope and explicit commit-date witness."""

import hashlib
import json
from pathlib import Path

import pytest

from headless_codex.services.analysis_source_capture import capture_sources


def fixture():
    data = json.loads((Path(__file__).parent / "fixtures/github_two_text_blocks.json").read_text())
    args = data["receipts"][0]["arguments"]
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "commit-date-fixture",
                "type": "mcp_tool_call",
                "server": "github",
                "tool": "get_commit",
                "arguments": {"owner": args["owner"], "repo": args["repo"], "sha": args["ref"]},
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"sha": args["ref"], "commit": {"committer": {"date": "2026-09-18T00:00:00Z"}}}
                            ),
                        }
                    ]
                },
            },
        }
    ]
    for receipt in data["receipts"]:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": receipt["source_ref"],
                    "type": "mcp_tool_call",
                    "server": "github",
                    "tool": receipt["tool_name"],
                    "arguments": receipt["arguments"],
                    "result": receipt["result"],
                },
            }
        )
    incident = {
        "alarm": {"StateChangeTime": "2026-09-18T15:00:00Z"},
        "observations": {
            phase: {"observations": [{"message": {"event": "source_manifest", **manifest}}]}
            for phase, manifest in data["manifests"].items()
        },
    }
    return events, incident


def capture(events, incident):
    return capture_sources("\n".join(json.dumps(event) for event in events), incident)["sources"]


def test_actual_file_bodies_preserve_manifest_phase_and_exact_text():
    events, incident = fixture()
    sources = capture(events, incident)
    assert [source["source_phase"] for source in sources] == ["incident", "normal_baseline"]
    for source, event in zip(sources, events[1:], strict=True):
        assert source["text"] == event["item"]["result"]["content"][1]["text"]
        assert source["path"] == event["item"]["arguments"]["path"]


@pytest.mark.parametrize("mutation", ["blob", "body", "branch", "error", "extra", "path", "date", "manifest"])
def test_actual_shape_still_requires_commit_blob_path_and_deployed_source(mutation):
    events, incident = fixture()
    events = events[:2]
    item = events[1]["item"]
    if mutation == "blob":
        item["result"]["content"][0]["text"] = f"successfully downloaded text file (SHA: {'0' * 40})"
    elif mutation == "body":
        item["result"]["content"][1]["text"] += "\n"
    elif mutation == "branch":
        item["arguments"]["ref"] = "main"
    elif mutation == "error":
        item["result"]["isError"] = True
    elif mutation == "extra":
        item["result"]["content"].append({"text": "ambiguous"})
    elif mutation == "path":
        item["arguments"]["path"] = "unrelated.py"
    elif mutation == "date":
        events = events[1:]
    else:
        incident["observations"]["current"] = {}
    assert capture(events, incident) == []


@pytest.mark.parametrize("mode", ["valid", "wrong_outer_sha", "extra", "malformed_header", "unknown_header", "legacy"])
def test_json_file_body_cannot_override_outer_blob_authority(mode):
    """Even a valid inner blob must not bypass the downloaded file's SHA."""
    events, incident = fixture()
    events = events[:2]
    item = events[1]["item"]
    item["arguments"]["path"] = "package.json"
    inner = "unbound embedded value"
    inner_sha = hashlib.sha1(f"blob {len(inner)}\0".encode() + inner.encode()).hexdigest()
    body = json.dumps({"type": "file", "path": "package.json", "encoding": "utf-8", "content": inner, "sha": inner_sha})
    outer_sha = hashlib.sha1(f"blob {len(body.encode())}\0".encode() + body.encode()).hexdigest()
    item["result"]["content"] = [
        {"type": "text", "text": f"successfully downloaded text file (SHA: {outer_sha})"},
        {"type": "text", "text": body},
    ]
    # Both source bytes are explicitly supplied, so only envelope/blob authority
    # can reject the inner substitute; manifest absence cannot mask the bug.
    incident["source_artifacts"] = [
        {"path": "package.json", "sha256": hashlib.sha256(value.encode()).hexdigest()} for value in (body, inner)
    ]
    if mode == "wrong_outer_sha":
        item["result"]["content"][0]["text"] = f"successfully downloaded text file (SHA: {'0' * 40})"
    elif mode == "extra":
        item["result"]["content"].append({"type": "text", "text": body})
    elif mode == "malformed_header":
        item["result"]["content"][0]["text"] += " unexpected annotation"
    elif mode == "unknown_header":
        item["result"]["content"][0]["text"] = "file download status unknown"
    elif mode == "legacy":
        item["result"]["content"] = [{"type": "text", "text": body}]
    sources = capture(events, incident)
    if mode in {"valid", "legacy"}:
        assert len(sources) == 1
        assert sources[0]["text"] == (body if mode == "valid" else inner)
    else:
        assert sources == []
