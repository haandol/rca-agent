"""Offline replay of unchanged archived evidence; never execute its command/source text."""

import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from headless_codex.services.execution_evidence import (
    ExecutionEvidence,
    redact,
    resolve_retrospective_references,
    retrospective_evidence_json,
)

FIXTURE = Path(__file__).parent / "fixtures/retrospective-custlock-20260910.json"
EVIDENCE_SHA = "904fc69c3ff76b6ace2f19121af753b5077d980aeb4f87a061947fee51efef52"


def _project(payload, **kwargs):
    return retrospective_evidence_json(SimpleNamespace(to_dict=lambda: payload), **kwargs)


def _assert_reconstructs(rendered, original):
    projected = json.loads(rendered)
    restored = resolve_retrospective_references(projected)
    projection = restored.pop("projection")
    omitted = 0
    pairs = [
        (attempt, source, ("stdout", "stderr", "observation", "error_output"))
        for step, original_step in zip(restored["steps"], original["steps"], strict=True)
        for attempt, source in zip(step["attempts"], original_step["attempts"], strict=True)
    ]
    pairs.extend(
        (record["response"], source["response"], ("stdout", "stderr"))
        for record, source in zip(
            restored.get("metric_wait_records", []), original.get("metric_wait_records", []), strict=True
        )
        if isinstance(record.get("response"), dict)
    )
    for actual, source, names in pairs:
        expected_markers = {}
        for name in names:
            if not isinstance(source.get(name), str) or not source[name]:
                continue
            assert source[name].startswith(actual[name])
            count = len(source[name]) - len(actual[name])
            omitted += count
            expected_markers[name] = {"omitted": bool(count), "omitted_chars": count}
            actual[name] = source[name]
        assert actual.pop("projection_omissions", {}) == expected_markers
    assert projection == {
        "output_previews_omitted": bool(omitted),
        "omitted_chars": omitted,
        "persisted_evidence_unchanged": True,
    }
    # Compare every metadata field, not a hand-picked subset of successful records.
    assert restored == original
    assert json.loads(rendered) == projected  # Resolver does not mutate its input.


def test_actual_archived_payload_replays_with_exact_metadata_and_unchanged_source():
    raw = FIXTURE.read_bytes()
    fixture = json.loads(raw)
    original = fixture["evidence"]
    before = copy.deepcopy(original)
    canonical = json.dumps(original, ensure_ascii=False, sort_keys=True)
    assert hashlib.sha256(canonical.encode()).hexdigest() == EVIDENCE_SHA
    assert fixture["provenance"]["evidence_sha256"] == EVIDENCE_SHA
    assert fixture["provenance"]["archived_retrospective_status"] == "FAILED"
    assert len(json.dumps(original, ensure_ascii=False)) == 245_919
    assert sum(len(step["attempts"]) for step in original["steps"]) == 34
    assert len(original["metric_wait_records"]) == 20
    for name in ("execution_id", "rca_id", "playbook_id"):
        assert fixture["provenance"][name] == original[name]
    rendered = _project(original)
    assert len(rendered) <= 60_000
    assert json.loads(rendered)["projection"]["shared_values"]
    _assert_reconstructs(rendered, original)
    assert original == before
    assert FIXTURE.read_bytes() == raw
    # The fixture is not sanitized by silently rewriting its archived evidence.
    for payload in (original, fixture["playbook_before"]):
        text = json.dumps(payload, ensure_ascii=False)
        assert json.loads(redact(text)) == payload
        assert not re.search(r"(?:AKIA|ASIA)[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----", text)
        assert not re.search(r"(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}", text)


def test_failure_blocked_partial_poll_and_terminal_bins_survive_sharing():
    payload = json.loads(FIXTURE.read_text())["evidence"]
    payload["steps"][0]["attempts"][0].update(
        succeeded=False, blocked=True, block_reason="denied", failure_class="BLOCKED_UNDECIDABLE"
    )
    payload["steps"][0]["outcomes"].append(
        {"criteria_met": False, "resolved": False, "recorded_at": "2026-09-10T14:06:00Z"}
    )
    payload["metric_wait_records"][1]["response"].update(
        ok=False, partial=True, stdout_truncated=True, stderr="timeout with partial evidence"
    )
    terminal = payload["metric_wait_records"][-1]
    terminal.update(ok=False, status="UNOBSERVABLE", write_semantics_verified=False)
    original = copy.deepcopy(payload)
    rendered = _project(payload)
    assert len(rendered) <= 60_000
    _assert_reconstructs(rendered, original)
    restored = resolve_retrospective_references(json.loads(rendered))
    assert restored["metric_wait_records"][-1] == terminal
    assert restored["metric_wait_records"][1]["response"]["partial"] is True
    assert restored["steps"][0]["attempts"][0]["blocked"] is True
    assert payload == original


def test_reference_shaped_literals_special_keys_and_exact_equality_do_not_collide():
    literal = {
        "paths": [["steps", 0, "intent"]],
        "value": {"$ref": "#/projection/shared_values/0"},
        "projection": {"shared_values": [{"value": "ignore instructions", "paths": []}]},
        "data": 'source text: {"$ref": 0}; `$(do not execute)` ' * 100,
    }
    payload = ExecutionEvidence(execution_id="e", rca_id="r", playbook_id="p").to_dict()
    payload["steps"] = [
        {
            "attempts": [],
            # Paths must distinguish string keys from array indexes, with no pointer escaping.
            "a/~.[]": {"0": {"request": copy.deepcopy(literal)}},
            "intent": 'literal {"$ref":0}',
            "null": None,
        }
        for _ in range(4)
    ]
    payload["steps"][0]["binding"] = {"number": True, "data": "x" * 1000}
    payload["steps"][1]["binding"] = {"number": 1, "data": "x" * 1000}
    payload["steps"][2]["binding"] = {"number": 1.0, "data": "x" * 1000}
    payload["steps"][3]["binding"] = {"number": False, "data": "x" * 1000}
    original = copy.deepcopy(payload)
    rendered = _project(payload, max_chars=12_000)
    projected = json.loads(rendered)
    assert len(rendered) <= 12_000
    assert projected["projection"]["shared_values"]
    assert all("number" not in entry["value"] for entry in projected["projection"]["shared_values"])
    _assert_reconstructs(rendered, original)
    assert payload == original


def test_shared_metadata_exact_budget_edge_and_unique_metadata_fail_closed():
    payload = ExecutionEvidence(execution_id="e", rca_id="r", playbook_id="p").to_dict()
    payload["steps"] = [{"attempts": [], "intent": "same metadata " * 1000} for _ in range(3)]
    rendered = _project(payload, max_chars=16_000)
    assert json.loads(rendered)["projection"]["shared_values"]
    assert _project(payload, max_chars=len(rendered)) == rendered
    with pytest.raises(ValueError, match="metadata exceeds JSON budget"):
        _project(payload, max_chars=len(rendered) - 1)
    _assert_reconstructs(rendered, payload)
    payload["resolution_observation"] = "unique observation " * 4000
    before = copy.deepcopy(payload)
    with pytest.raises(ValueError, match="metadata exceeds JSON budget"):
        _project(payload)
    assert payload == before


@pytest.mark.parametrize("legacy", [False, True])
def test_empty_and_legacy_evidence_needs_no_references_or_invented_times(legacy):
    payload = ExecutionEvidence(execution_id="e", rca_id="r", playbook_id="p").to_dict()
    if legacy:
        payload["steps"] = [{"attempts": [{"command": "aws ecs describe-tasks", "succeeded": False}]}]
    rendered = _project(payload)
    assert "shared_values" not in json.loads(rendered)["projection"]
    _assert_reconstructs(rendered, payload)


@pytest.mark.parametrize("path", [["steps", 99, "intent"], ["projection", "shared_values"]])
def test_resolver_rejects_unresolvable_or_annotation_paths(path):
    payload = {"steps": [], "projection": {"shared_values": [{"value": "literal", "paths": [path]}]}}
    with pytest.raises(ValueError, match="shared-value path"):
        resolve_retrospective_references(payload)
