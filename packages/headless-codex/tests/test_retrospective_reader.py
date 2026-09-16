"""Actual archived Case10 bytes, offline transport. These tests are not a live resolution."""

import asyncio
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from headless_codex import retrospective_mcp_server as server
from headless_codex.ports.interfaces.execution_store import ExecutionTarget
from headless_codex.services import retrospective_reader as reader
from headless_codex.services.execution_evidence import retrospective_evidence_json
from headless_codex.services.execution_prompt import build_retrospective_prompt

FIXTURE = Path(__file__).parent / "fixtures/retrospective-live10-evidence.json.gz"


def test_actual_case10_original_reproduces_old_failure_then_scoped_mcp_pages_exactly(retrospective_sources):
    raw = gzip.decompress(FIXTURE.read_bytes())
    assert len(raw) == 1487010
    assert hashlib.sha256(raw).hexdigest() == "62252e305fbe6a6cec0df4f5c8d72d08ef090a281797e970cbe686e89e85fa6f"
    original = json.loads(raw)
    with pytest.raises(ValueError, match="metadata exceeds JSON budget"):
        retrospective_evidence_json(SimpleNamespace(to_dict=lambda: original))
    work = retrospective_sources.prepare(original["rca_id"], original["execution_id"], original)
    ref, _ = reader._reference(work.token, work.execution_id)
    retrospective_sources.objects[ref["documents"]["evidence"]] = raw
    prompt = build_retrospective_prompt(
        ExecutionTarget(rca_id=original["rca_id"], engine="strands", alarm_name="archived", playbook={}),
        execution_id=work.execution_id,
        evidence_key=ref["documents"]["evidence"],
        approved_playbook_key=ref["documents"]["approved_playbook"],
    )
    assert len(prompt) < 4000
    assert "```json" not in prompt

    async def replay():
        async with Client(server.mcp) as client:
            tool = next(t for t in await client.list_tools() if t.name == "read_retrospective_document")
            assert set(tool.inputSchema["properties"]) == {"document", "pointer", "offset", "max_items", "text_chars"}

            # Reconstruct every field, including commands/denials/failures/terminal proofs and all refs.
            async def restore(pointer=""):
                offset = 0
                items = []
                text = ""
                while True:
                    result = await client.call_tool(
                        "read_retrospective_document",
                        {"document": "evidence", "pointer": pointer, "offset": offset, "max_items": 50},
                    )
                    response = json.loads(result.content[0].text)
                    assert response["ok"], response
                    assert len(result.content[0].text) <= 12000
                    kind = response["kind"]
                    if kind == "scalar":
                        return response["value"]
                    if kind == "string":
                        text += response["text"]
                    else:
                        items.extend(response["items"])
                    if response["complete"]:
                        break
                    assert response["next_offset"] > offset
                    offset = response["next_offset"]
                if kind == "string":
                    return text
                values = []
                for item in items:
                    values.append(item["value"] if item.get("complete") else await restore(item["pointer"]))
                return dict(zip([i["key"] for i in items], values, strict=True)) if kind == "object" else values

            assert await restore() == original
            response = await client.call_tool("read_retrospective_document", {"document": "approved_playbook"})
            assert json.loads(response.content[0].text)["ok"]
            response = await client.call_tool(
                "save_playbook_update", {"update_json": "{}", "rationale": "Offline transport reconstruction only"}
            )
            assert json.loads(response.content[0].text)["ok"]

    asyncio.run(replay())
    assert retrospective_sources.objects[ref["documents"]["evidence"]] == raw
    assert json.loads(work.retrospective_path.read_text())["update"] == {}


@pytest.mark.parametrize(
    "mode",
    [
        "no_read",
        "missing",
        "wrong_rca",
        "wrong_execution",
        "unresolved",
        "wrong_digest",
        "playbook_mismatch",
        "cache_changed",
        "bad_ref",
        "symlink",
    ],
)
def test_missing_wrong_or_failed_reads_cannot_save_or_promote(retrospective_sources, mode):
    work = retrospective_sources.prepare()
    ref, _ = reader._reference(work.token, work.execution_id)
    if mode == "playbook_mismatch":
        source = json.loads(retrospective_sources.objects[ref["documents"]["evidence"]])
        source["playbook_id"] = "different-playbook"
        retrospective_sources.objects[ref["documents"]["evidence"]] = json.dumps(source).encode()
    if mode == "missing":
        del retrospective_sources.objects[ref["documents"]["evidence"]]
    elif mode in ("wrong_rca", "wrong_execution", "unresolved"):
        source = json.loads(retrospective_sources.objects[ref["documents"]["evidence"]])
        source[{"wrong_rca": "rca_id", "wrong_execution": "execution_id", "unresolved": "final_state"}[mode]] = (
            "different"
        )
        retrospective_sources.objects[ref["documents"]["evidence"]] = json.dumps(source).encode()
    elif mode == "wrong_digest":
        retrospective_sources.objects[ref["documents"]["approved_playbook"]] = b'{"changed":true}'
    elif mode == "bad_ref":
        ref["documents"]["evidence"] = "executions/other/exec/evidence.json"
        (work.path / "retrospective-reference.json").write_text(json.dumps(ref))
    elif mode == "symlink":
        (work.path / "retrospective-evidence-cache.json").symlink_to(work.evidence_path)
    if mode != "no_read":
        for document in ("evidence", "approved_playbook"):
            reader.read_document(work.token, work.execution_id, document)
    if mode == "cache_changed":
        cache = work.path / "retrospective-evidence-cache.json"
        source = json.loads(cache.read_text())
        source["new"] = "forged"
        cache.write_text(json.dumps(source))
    assert not json.loads(server.save_playbook_update("{}", "cannot attest"))["ok"]
    with pytest.raises((ValueError, FileNotFoundError)):
        reader.require_successful_reads(work.token, work.execution_id)
    assert not work.retrospective_path.exists()


def test_large_index_and_text_have_explicit_page_or_error(retrospective_sources):
    work = retrospective_sources.prepare(
        evidence={
            "rca_id": "rca",
            "execution_id": "exec",
            "final_state": "RESOLVED",
            "resolution_confirmed": True,
            "text": '\\"\n' * 9000,
            "x" * 14000: 1,
        }
    )
    text = reader.read_document(work.token, "exec", "evidence", "/text")
    assert text["ok"] and not text["complete"] and text["next_offset"] > 0
    assert len(json.dumps(text, ensure_ascii=False)) <= 12000
    root = reader.read_document(work.token, "exec", "evidence")
    assert root["ok"] and not root["complete"]
    failure = reader.read_document(work.token, "exec", "evidence", offset=root["next_offset"])
    assert not failure["ok"] and "budget" in failure["detail"]


def test_foreign_manifest_rejected_before_s3(retrospective_sources, monkeypatch):
    work = retrospective_sources.prepare()
    monkeypatch.setattr(reader, "_s3_client", lambda: pytest.fail("foreign ref reached S3"))
    ref, _ = reader._reference(work.token, "exec")
    ref["bucket"] = "other-bucket"
    (work.path / "retrospective-reference.json").write_text(json.dumps(ref))
    assert not reader.read_document(work.token, "exec", "evidence")["ok"]


@pytest.mark.parametrize(
    "arguments",
    [{"pointer": "/missing"}, {"pointer": "/~invalid"}, {"offset": 10000}, {"max_items": 100}, {"document": "unknown"}],
)
def test_selector_error_allows_correct_same_source_reads_and_save(retrospective_sources, arguments):
    work = retrospective_sources.prepare()
    reply = reader.read_document(work.token, work.execution_id, **{"document": "evidence", **arguments})
    assert not reply["ok"] and reply["recoverable"]
    assert not json.loads(server.save_playbook_update("{}", "not yet read"))["ok"]
    retrospective_sources.read_both(work)
    assert json.loads(server.save_playbook_update("{}", "corrected selector then verified both originals"))["ok"]


def test_source_failure_stays_failed_after_transport_recovers(retrospective_sources):
    work = retrospective_sources.prepare()
    ref, _ = reader._reference(work.token, work.execution_id)
    key = ref["documents"]["evidence"]
    original = retrospective_sources.objects.pop(key)
    assert not reader.read_document(work.token, work.execution_id, "evidence")["recoverable"]
    retrospective_sources.objects[key] = original
    assert not reader.read_document(work.token, work.execution_id, "evidence")["ok"]
    assert not json.loads(server.save_playbook_update("{}", "source failed"))["ok"]
