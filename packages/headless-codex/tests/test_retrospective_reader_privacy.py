"""R1/R2 public MCP regressions: synthetic canaries, exact private originals, bounded model views."""

import asyncio
import hashlib
import json

import pytest
from fastmcp import Client

from headless_codex import retrospective_mcp_server as server
from headless_codex.services import retrospective_reader as reader

CANARY = "SYNTHETIC_PRIVATE_RETROSPECTIVE_CANARY"


def call(document, pointer="", **kwargs):
    async def run():
        async with Client(server.mcp) as client:
            response = await client.call_tool(
                "read_retrospective_document", {"document": document, "pointer": pointer, **kwargs}
            )
            text = response.content[0].text
            assert len(text) <= 12000
            assert CANARY not in text
            return json.loads(text)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "field,value,pointer",
    [
        ("temporary_mitigation", "api_key=" + CANARY, "/temporary_mitigation"),
        ("password", CANARY, "/password"),
        ("environment", [{"name": "AWS_SECRET_ACCESS_KEY", "value": CANARY}], "/environment/0/value"),
        ("environment", [{"value": CANARY, "name": "DB_PASSWORD"}], "/environment/0/value"),
        ("environment", [{"key": "API_TOKEN", "value": CANARY}], "/environment/0/value"),
        ("commands", ['aws example operation --password "' + CANARY + '" --region us-east-1'], "/commands/0"),
        ("commands", ["DB_PASSWORD=" + CANARY + " aws example operation"], "/commands/0"),
        ("observation", "Authorization: Bearer " + CANARY, "/observation"),
        ("observation", "postgres://user:" + CANARY + "@host/database", "/observation"),
        ("observation", json.dumps({"environment": [{"name": "DB_PASSWORD", "value": CANARY}]}), "/observation"),
    ],
)
def test_structured_fields_env_and_commands_are_redacted_before_public_selection(
    retrospective_sources, field, value, pointer
):
    work = retrospective_sources.prepare(playbook={"playbook_id": "pb", field: value, "nextToken": "public-page-token"})
    ref, _ = reader._reference(work.token, work.execution_id)
    original = retrospective_sources.objects[ref["documents"]["approved_playbook"]]
    assert CANARY.encode() in original
    assert call("evidence")["ok"]
    # Root index short scalar values and direct leaf access must have the same privacy boundary.
    assert call("approved_playbook")["ok"]
    reply = call("approved_playbook", pointer)
    assert reply["ok"] and "***REDACTED***" in reply["text"]
    assert reply["source"]["view"] == "redacted_stored_json"
    assert reply["source"]["offset_scope"] == "redacted_value"
    assert reply["source"]["sha256_scope"] == "original_stored_bytes"
    assert reply["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert call("approved_playbook", "/nextToken")["text"] == "public-page-token"
    assert (work.path / "retrospective-approved_playbook-cache.json").read_bytes() == original
    assert retrospective_sources.objects[ref["documents"]["approved_playbook"]] == original
    reader.require_successful_reads(work.token, work.execution_id)


@pytest.mark.parametrize("prefix_length", [0, 3970, 3998, 4000, 7995])
def test_whole_string_redaction_precedes_every_chunk_boundary(retrospective_sources, prefix_length):
    raw_text = "x" * prefix_length + ' api_key="' + CANARY + '" trailing observed proof'
    work = retrospective_sources.prepare(playbook={"playbook_id": "pb", "temporary_mitigation": raw_text})
    offset = 0
    parts = []
    while True:
        response = call("approved_playbook", "/temporary_mitigation", offset=offset, text_chars=4000)
        assert response["ok"]
        parts.append(response["text"])
        if response["complete"]:
            break
        assert response["next_offset"] > offset
        offset = response["next_offset"]
    assert "".join(parts) == "x" * prefix_length + ' api_key="***REDACTED***" trailing observed proof'
    assert CANARY in (work.path / "retrospective-approved_playbook-cache.json").read_text()


@pytest.mark.parametrize("value", [1, True, None, {}, [], ""])
def test_all_selected_types_with_oversized_key_return_short_explicit_error(retrospective_sources, value):
    key = "x" * 14000
    retrospective_sources.prepare(
        evidence={
            "rca_id": "rca",
            "execution_id": "exec",
            "playbook_id": "pb",
            "final_state": "RESOLVED",
            "resolution_confirmed": True,
            key: value,
        }
    )
    reply = call("evidence", "/" + key)
    assert not reply["ok"] and reply["recoverable"]
    assert "budget" in reply["detail"]
    assert key not in json.dumps(reply)
    assert call("evidence", "/final_state")["text"] == "RESOLVED"


def test_long_missing_pointer_is_short_error_not_source_loss(retrospective_sources):
    work = retrospective_sources.prepare()
    reply = call("evidence", "/" + "x" * 14000)
    assert not reply["ok"] and reply["recoverable"]
    retrospective_sources.read_both(work)
    reader.require_successful_reads(work.token, work.execution_id)


def test_source_error_redacts_entire_detail_before_shortening(retrospective_sources, monkeypatch):
    work = retrospective_sources.prepare()

    def fail(**kwargs):
        raise KeyError("x" * 480 + ' api_key="' + CANARY + '"' + "x" * 14000)

    monkeypatch.setattr(retrospective_sources.client, "get_object", fail)
    reply = call("evidence")
    assert not reply["ok"] and not reply["recoverable"]
    with pytest.raises(ValueError):
        reader.require_successful_reads(work.token, work.execution_id)


def test_secret_in_key_is_not_leaked_or_silently_renamed(retrospective_sources):
    key = "api_key=" + CANARY
    retrospective_sources.prepare(playbook={"playbook_id": "pb", key: 1})
    for pointer in ("", "/" + key):
        reply = call("approved_playbook", pointer)
        assert not reply["ok"] and reply["recoverable"]
        assert "sensitive" in reply["detail"]


def test_two_oversized_queries_cannot_record_success_or_authorize_no_change(retrospective_sources):
    key = "x" * 14000
    work = retrospective_sources.prepare(
        evidence={
            "rca_id": "rca",
            "execution_id": "exec",
            "playbook_id": "pb",
            "final_state": "RESOLVED",
            "resolution_confirmed": True,
            key: 1,
        },
        playbook={"playbook_id": "pb", key: 2},
    )
    for document in ("evidence", "approved_playbook"):
        response = call(document, "/" + key)
        assert not response["ok"] and response["recoverable"]
    _, fingerprint = reader._reference(work.token, work.execution_id)
    assert reader._state(work.token, fingerprint)["documents_read"] == []
    assert not json.loads(server.save_playbook_update("{}", "both queries failed"))["ok"]
    with pytest.raises(ValueError):
        reader.require_successful_reads(work.token, work.execution_id)
    for document in ("evidence", "approved_playbook"):
        assert call(document, "/playbook_id")["ok"]
    assert json.loads(server.save_playbook_update("{}", "both sources now read successfully"))["ok"]


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_url_credentials_in_keys_are_checked_before_pointer_encoding(retrospective_sources, direct, nested):
    key = "https://user:" + CANARY + "@host/database"
    content = {key: "record"}
    book = {"playbook_id": "pb", **({"section/~": content} if nested else content)}
    work = retrospective_sources.prepare(playbook=book)
    ref, _ = reader._reference(work.token, work.execution_id)
    original = retrospective_sources.objects[ref["documents"]["approved_playbook"]]
    parent = "/section~1~0" if nested else ""
    pointer = parent + "/" + key.replace("~", "~0").replace("/", "~1") if direct else parent
    reply = call("approved_playbook", pointer)
    assert not reply["ok"] and reply["recoverable"] and "sensitive" in reply["detail"]
    _, fingerprint = reader._reference(work.token, work.execution_id)
    assert reader._state(work.token, fingerprint)["documents_read"] == []
    assert (work.path / "retrospective-approved_playbook-cache.json").read_bytes() == original
    assert retrospective_sources.objects[ref["documents"]["approved_playbook"]] == original
    assert hashlib.sha256(original).hexdigest() == ref["playbook_digest"]


def test_benign_url_and_literal_escaped_keys_preserve_exact_references(retrospective_sources):
    key = "https://host/database/~1/~0"
    retrospective_sources.prepare(playbook={"playbook_id": "pb", key: "observed value"})
    root = call("approved_playbook")
    entry = next(item for item in root["items"] if item["key"] == key)
    assert entry["pointer"] == "/" + key.replace("~", "~0").replace("/", "~1")
    assert call("approved_playbook", entry["pointer"])["text"] == "observed value"
