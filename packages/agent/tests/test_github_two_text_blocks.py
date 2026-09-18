"""Replay actual live GitHub source receipts; synthetic mutations must not become evidence."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from rca_agent.services.frozen_evidence import received_control_artifacts, received_source_artifacts


def fixture():
    data = json.loads((Path(__file__).parent / "fixtures/github_two_text_blocks.json").read_text())
    observations = {
        phase: {"observations": [{"message": {"event": "source_manifest", **manifest}}]}
        for phase, manifest in data["manifests"].items()
    }
    return data["receipts"], SimpleNamespace(incident_observations=SimpleNamespace(**observations))


def test_actual_live_normal_and_fault_text_blocks_retain_exact_bytes_and_phase():
    receipts, scope = fixture()
    before = deepcopy(receipts)
    artifacts = received_source_artifacts(receipts, scope)
    assert len(artifacts) == 2
    assert [a["source_phase"] for a in artifacts] == ["current", "baseline"]
    for artifact, receipt in zip(artifacts, receipts, strict=True):
        assert artifact["text"] == receipt["result"]["content"][1]["text"]
        assert artifact["source_ref"] == receipt["source_ref"]
        assert artifact["base_ref"] == receipt["arguments"]["ref"]
        assert artifact["path"] == receipt["arguments"]["path"]
    assert receipts == before


@pytest.mark.parametrize("mutation", ["blob", "body", "branch", "failed", "active", "extra", "path", "manifest"])
def test_two_blocks_do_not_bypass_existing_source_guards(mutation):
    receipts, scope = fixture()
    receipt = receipts[0]
    if mutation == "blob":
        receipt["result"]["content"][0]["text"] = f"successfully downloaded text file (SHA: {'0' * 40})"
    elif mutation == "body":
        receipt["result"]["content"][1]["text"] += "\n"
    elif mutation == "branch":
        receipt["arguments"]["ref"] = "main"
    elif mutation == "failed":
        receipt["result"]["isError"] = True
    elif mutation == "active":
        receipt["request_terminated"] = False
    elif mutation == "extra":
        receipt["result"]["content"].append({"text": "ambiguous extra body"})
    elif mutation == "path":
        receipt["arguments"]["path"] = "unrelated.py"
    else:
        scope.incident_observations.current["observations"] = []
    assert received_source_artifacts([receipt], scope) == []


def test_same_provider_shape_for_ci_is_control_evidence_only():
    """Synthetic CI response uses the observed transport shape, not a claimed live CI read."""
    receipts, scope = fixture()
    receipt = receipts[0]
    text = "name: CI\non: [push]\n"
    blob = hashlib.sha1(f"blob {len(text.encode())}\0".encode() + text.encode()).hexdigest()
    receipt["arguments"]["path"] = ".github/workflows/test.yml"
    receipt["result"]["content"] = [
        {"text": f"successfully downloaded text file (SHA: {blob})"},
        {"text": text},
    ]
    assert received_control_artifacts([receipt])[0]["text"] == text
    assert received_source_artifacts([receipt], scope) == []
    receipt["result"]["content"][1]["text"] += "tampered"
    assert received_control_artifacts([receipt]) == []


@pytest.mark.parametrize("mode", ["valid", "wrong_outer_sha", "extra", "malformed_header", "unknown_header", "legacy"])
def test_downloaded_json_is_opaque_and_outer_sha_is_authoritative(mode):
    """A source file containing valid Contents API JSON must not supply an inner substitute file."""
    receipts, _ = fixture()
    receipt = receipts[0]
    receipt["arguments"]["path"] = "package.json"
    inner = "unbound embedded value"
    inner_sha = hashlib.sha1(f"blob {len(inner)}\0".encode() + inner.encode()).hexdigest()
    body = json.dumps({"type": "file", "path": "package.json", "encoding": "utf-8", "content": inner, "sha": inner_sha})
    outer_sha = hashlib.sha1(f"blob {len(body.encode())}\0".encode() + body.encode()).hexdigest()
    receipt["result"]["content"] = [
        {"text": f"successfully downloaded text file (SHA: {outer_sha})"},
        {"text": body},
    ]
    if mode == "wrong_outer_sha":
        receipt["result"]["content"][0]["text"] = f"successfully downloaded text file (SHA: {'0' * 40})"
    elif mode == "extra":
        receipt["result"]["content"].append({"text": body})
    elif mode == "malformed_header":
        receipt["result"]["content"][0]["text"] += " unexpected annotation"
    elif mode == "unknown_header":
        receipt["result"]["content"][0]["text"] = "file download status unknown"
    elif mode == "legacy":
        receipt["result"]["content"] = [{"text": body}]
    result = received_control_artifacts([receipt])
    if mode in {"valid", "legacy"}:
        assert len(result) == 1
        assert result[0]["text"] == (body if mode == "valid" else inner)
    else:
        assert result == []
