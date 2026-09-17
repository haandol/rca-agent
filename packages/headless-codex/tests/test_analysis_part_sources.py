"""Actual source/CI-shaped receipts, with only injected local transports and no repository writes."""

import base64
import copy
import difflib
import hashlib
import io
import json

import pytest

from headless_codex.services.analysis_part_contract import validate_code_proposal, validate_recovery_narrative
from headless_codex.services.analysis_source_capture import capture_sources
from headless_codex.services.operations_sources import qualify_operations, read_ci_source


def source_receipts(text="bad = 1\n"):
    revision = "a" * 40
    raw = text.encode()
    blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()

    def event(tool, args, body):
        return json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": tool,
                    "type": "mcp_tool_call",
                    "server": "github",
                    "tool": tool,
                    "arguments": args,
                    "result": {"content": [{"type": "text", "text": json.dumps(body)}]},
                },
            }
        )

    calls = [
        event(
            "get_commit",
            {"owner": "o", "repo": "r", "sha": revision},
            {"sha": revision, "commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}},
        ),
        event(
            "get_file_contents",
            {"owner": "o", "repo": "r", "ref": revision, "path": "src/app.py"},
            {
                "type": "file",
                "path": "src/app.py",
                "sha": blob,
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            },
        ),
    ]
    incident = {"alarm": {"StateChangeTime": "2026-09-02T00:00:00Z"}, "observations": {"source_revision": revision}}
    return "\n".join(calls), incident


def test_actual_pinned_git_receipt_supplies_verified_code_preview():
    output, incident = source_receipts()
    captured = capture_sources(output, incident)
    assert len(captured["sources"]) == 1
    source = captured["sources"][0]
    original = source["text"]
    proposed = "bad = 2\n"
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile="a/src/app.py",
            tofile="b/src/app.py",
        )
    )
    preview = {
        "status": "PROPOSED",
        "title": "제안",
        "repository": "o/r",
        "base_revision": "a" * 40,
        "files": [
            {
                "path": "src/app.py",
                "start_line": 1,
                "end_line": 1,
                "original": original,
                "proposed": proposed,
                "unified_diff": diff,
                "evidence_refs": [source["source_ref"]],
            }
        ],
        "test_plan": ["단위 테스트"],
        "tests_status": "NOT_RUN",
        "limitations": [],
    }
    assert validate_code_proposal(preview, captured["sources"]) == preview
    preview["files"][0]["original"] = "invented\n"
    assert validate_code_proposal(preview, captured["sources"])["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("change", ["after_cutoff", "wrong_deployed_revision", "bad_blob", "source_text_as_commit"])
def test_unverified_code_never_becomes_deployed_source(change):
    output, incident = source_receipts()
    if change == "after_cutoff":
        output = output.replace("2026-09-01", "2026-09-03")
    elif change == "wrong_deployed_revision":
        incident["observations"]["source_revision"] = "b" * 40
    elif change == "bad_blob":
        output = output.replace("YmFkID0gMQo=", base64.b64encode(b"other\n").decode())
    else:
        lines = output.splitlines()
        event = json.loads(lines[0])
        body = json.loads(event["item"]["result"]["content"][0]["text"])
        event["item"]["result"]["content"][0]["text"] = json.dumps({"message": json.dumps(body)})
        output = json.dumps(event) + "\n" + lines[1]
    assert capture_sources(output, incident)["sources"] == []


def test_ci_get_is_associated_readonly_and_kept_separate_from_incident():
    text = "name: CI\n"
    raw = text.encode()
    sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    context = {"incident": {"source_artifacts": [{"repository": "o/r"}]}}
    before = copy.deepcopy(context)
    seen = []

    def fetch(request, timeout):
        seen.append((request.get_method(), request.full_url, timeout))
        return io.BytesIO(
            json.dumps(
                {
                    "type": "file",
                    "path": ".github/workflows/check.yml",
                    "encoding": "base64",
                    "sha": sha,
                    "content": base64.b64encode(raw).decode(),
                }
            ).encode()
        )

    source = read_ci_source(
        "o/r",
        ".github/workflows/check.yml",
        "main",
        context=context,
        token="test-only",
        configured_repository="",
        timeout=30,
        fetch=fetch,
    )
    assert seen[0][0] == "GET" and seen[0][1].startswith("https://api.github.com/repos/o/r/contents/")
    assert source["kind"] == "current_ci" and source["revision_kind"] == "git_blob_snapshot"
    assert context == before
    result = {
        "findings": [
            {"statement": "CI 정의 확인", "status": "OBSERVED", "evidence_refs": [source["source_ref"]]},
            {"statement": "다른 통제 없음", "status": "OBSERVED", "evidence_refs": ["invented"]},
        ],
        "limitations": [],
    }
    qualified = qualify_operations(result, [source])
    assert [item["status"] for item in qualified["findings"]] == ["OBSERVED", "UNVERIFIED"]
    assert qualify_operations(result, [])["findings"][0]["status"] == "UNVERIFIED"


def test_ci_foreign_repository_cannot_invoke_transport():
    with pytest.raises(ValueError, match="associated"):
        read_ci_source(
            "foreign/repo",
            ".github/workflows/check.yml",
            "main",
            context={},
            token="test-only",
            configured_repository="o/r",
            timeout=30,
            fetch=lambda *_a, **_k: pytest.fail("foreign request"),
        )


def test_recovery_model_cannot_supply_plan_or_approval_flags():
    value = {
        "title": "제목",
        "summary": "요약",
        "reason": "사유",
        "evidence_refs": [],
        "limitations": [],
        "recommendation": "UNAVAILABLE",
    }
    assert validate_recovery_narrative(value) == value
    for key in ("playbook", "verification", "approval_status"):
        with pytest.raises(ValueError):
            validate_recovery_narrative({**value, key: True})


def test_noop_or_correct_baseline_is_not_a_fault_fix_proposal():
    output, incident = source_receipts()
    source = capture_sources(output, incident)["sources"][0]
    original = source["text"]
    preview = {
        "status": "PROPOSED",
        "title": "fix",
        "files": [
            {
                "path": "src/app.py",
                "start_line": 1,
                "end_line": 1,
                "original": original,
                "proposed": original,
                "unified_diff": "invented nonempty diff",
                "evidence_refs": [source["source_ref"]],
            }
        ],
        "test_plan": [],
        "tests_status": "NOT_RUN",
        "limitations": [],
    }
    assert validate_code_proposal(preview, [source])["status"] == "UNAVAILABLE"
    preview["files"][0]["proposed"] = "bad = 2\n"
    preview["files"][0]["unified_diff"] = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True), ["bad = 2\n"], fromfile="a/src/app.py", tofile="b/src/app.py"
        )
    )
    source["source_phase"] = "normal_baseline"
    assert validate_code_proposal(preview, [source])["status"] == "UNAVAILABLE"
    source["source_phase"] = "supplied_snapshot"
    assert validate_code_proposal(preview, [source])["status"] == "PROPOSED"


def test_capture_does_not_relabel_normal_baseline_as_current_fault():
    output, incident = source_receipts()
    incident["observations"] = {"current": {"source_revision": "b" * 40}, "baseline": {"source_revision": "a" * 40}}
    source = capture_sources(output, incident)["sources"][0]
    assert source["source_phase"] == "normal_baseline"
    assert "@" + ("a" * 40) + "/" in source["source_ref"]


def test_github_mcp_pinned_version_embedded_resource_shape_is_captured():
    output, incident = source_receipts()
    events = [json.loads(line) for line in output.splitlines()]
    item = events[1]["item"]
    file = json.loads(item["result"]["content"][0]["text"])
    item["arguments"]["sha"] = item["arguments"].pop("ref")
    item["result"]["content"] = [
        {"type": "text", "text": f"successfully downloaded text file (SHA: {file['sha']})"},
        {
            "type": "resource",
            "resource": {
                "uri": "repo://o/r/sha/" + ("a" * 40) + "/contents/src/app.py",
                "mimeType": "text/plain",
                "text": "bad = 1\n",
            },
        },
    ]
    captured = capture_sources("\n".join(json.dumps(event) for event in events), incident)
    assert captured["sources"][0]["text"] == "bad = 1\n"
    assert captured["sources"][0]["base_revision"] == "a" * 40
    item["result"]["content"][0]["text"] += " Note: default branch was used instead."
    assert capture_sources("\n".join(json.dumps(event) for event in events), incident)["sources"] == []


def mapped_source_receipts():
    """A real-shaped immutable GitHub read of the checked-in build variant, with a runtime-relative manifest."""
    output, incident = source_receipts()
    path = "packages/healthcare-sensor-app/demo/revisions/v2/revision/write.py"
    output = output.replace("src/app.py", path)
    digest = hashlib.sha256(b"bad = 1\n").hexdigest()
    files = {"revision/write.py": digest}
    manifest = {
        "event": "source_manifest",
        "verified": True,
        "files": files,
        "fingerprint": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "source_locations": {
            "revision/write.py": {
                "repository": "o/r",
                "commit": "a" * 40,
                "path": path,
                "sha256": digest,
                "verification": "declared",
            }
        },
    }
    incident["observations"] = {"current": {"observations": [{"message": manifest}]}}
    return output, incident, manifest


def test_declared_location_binds_actual_immutable_read_to_exact_installed_hash():
    output, incident, _ = mapped_source_receipts()
    original = copy.deepcopy(incident)
    sources = capture_sources(output, incident)["sources"]
    assert len(sources) == 1 and sources[0]["source_phase"] == "incident"
    assert sources[0]["path"] == "packages/healthcare-sensor-app/demo/revisions/v2/revision/write.py"
    assert sources[0]["text"] == "bad = 1\n" and sources[0]["base_revision"] == "a" * 40
    assert incident == original


@pytest.mark.parametrize(
    "change", ["repo", "commit", "hash", "fingerprint", "suffix", "ambiguous", "no_read", "no_manifest"]
)
def test_declared_location_cannot_replace_actual_read_or_exact_binding(change):
    output, incident, manifest = mapped_source_receipts()
    location = manifest["source_locations"]["revision/write.py"]
    if change == "repo":
        location["repository"] = "other/repo"
    elif change == "commit":
        location["commit"] = "b" * 40
    elif change == "hash":
        location["sha256"] = "b" * 64
    elif change == "fingerprint":
        manifest["fingerprint"] = "b" * 64
    elif change == "suffix":
        location["path"] = "other/" + location["path"]
    elif change == "ambiguous":
        manifest["source_locations"]["other/write.py"] = dict(location)
    elif change == "no_read":
        output = output.splitlines()[0]
    else:
        manifest["verified"] = False
    assert capture_sources(output, incident)["sources"] == []


def test_normal_mapped_source_keeps_baseline_phase():
    output, incident, _ = mapped_source_receipts()
    incident["observations"]["baseline"] = incident["observations"].pop("current")
    assert capture_sources(output, incident)["sources"][0]["source_phase"] == "normal_baseline"


def test_actual_compiled_fault_file_maps_through_git_receipt_to_preview(tmp_path):
    import importlib.util
    from pathlib import Path

    package = Path(__file__).resolve().parents[2] / "healthcare-sensor-app"
    spec = importlib.util.spec_from_file_location("sensor_build_proof", package / "demo/build_revision.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    snapshot = tmp_path / "snapshot"
    builder.capture_source_snapshot(snapshot)
    manifest = builder.compile_revision(
        "v2", tmp_path / "installed", source_package=snapshot, source_repository="o/r", source_commit="a" * 40
    )
    text = (tmp_path / "installed/test_service/revision/write.py").read_text()
    assert text == (snapshot / "demo/revisions/v2/revision/write.py").read_text()
    path = manifest["source_locations"]["revision/write.py"]["path"]
    output, incident = source_receipts(text)
    output = output.replace("src/app.py", path)
    incident["observations"] = {
        "current": {"observations": [{"message": {**manifest, "event": "source_manifest", "verified": True}}]}
    }
    sources = capture_sources(output, incident)["sources"]
    assert len(sources) == 1 and sources[0]["text"] == text
    proposed = text.replace('TIMESTAMP_COLUMN = "sampled_at"', 'TIMESTAMP_COLUMN = "timestamp"')
    preview = {
        "status": "PROPOSED",
        "title": "실제 결함 원문 수정",
        "tests_status": "NOT_RUN",
        "files": [
            {
                "path": path,
                "start_line": 1,
                "end_line": len(text.splitlines()),
                "original": text,
                "proposed": proposed,
                "unified_diff": "".join(
                    difflib.unified_diff(
                        text.splitlines(keepends=True),
                        proposed.splitlines(keepends=True),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                    )
                ),
                "evidence_refs": [sources[0]["source_ref"]],
            }
        ],
        "test_plan": [],
        "limitations": [],
    }
    assert validate_code_proposal(preview, sources)["status"] == "PROPOSED"
