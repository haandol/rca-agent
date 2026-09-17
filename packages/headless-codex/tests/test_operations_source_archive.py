"""The UI source comes from this part's verified archive; declarations cannot replace original bytes."""

import base64
import copy
import hashlib
import io
import json

import pytest
from test_three_part_runtime import Scripted, context, run

from headless_codex.ports.dto.models import CodexResult
from headless_codex.services import execution_context
from headless_codex.services.analysis_part_workspace import RESULT_FILES, write_once
from headless_codex.services.analysis_parts import json_bytes
from headless_codex.services.analysis_root_archive import read_operations_sources
from headless_codex.services.operations_sources import read_ci_source, save_ci_source
from headless_codex.services.three_part_analysis import LocalPartStore


def source():
    text = "name: recorded-ci\njobs: {}\n"
    content = text.encode()
    blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    return read_ci_source(
        "owner/repo",
        ".github/workflows/check.yml",
        "main",
        context={},
        token="",
        configured_repository="owner/repo",
        timeout=10,
        fetch=lambda *_a, **_k: io.BytesIO(
            json.dumps(
                {
                    "type": "file",
                    "path": ".github/workflows/check.yml",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                    "sha": blob,
                }
            ).encode()
        ),
    )


def archive(store, payload):
    raw = json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    key = f"analysis-parts/{store.engine}/incident/operations/{digest}.json"
    store._put_object(key, raw)
    return {"key": key, "sha256": digest, "kind": "current_ci_sources"}


@pytest.mark.parametrize(
    "change", ["hash", "rca", "engine", "source_hash", "blob", "source_ref", "missing", "cross_key"]
)
def test_bad_archive_cannot_become_displayed_source(change):
    store = LocalPartStore()
    payload = {
        "schema_version": 1,
        "rca_id": "incident",
        "engine": store.engine,
        "kind": "current_ci_sources",
        "sources": [source()],
    }
    if change == "rca":
        payload["rca_id"] = "other"
    if change == "engine":
        payload["engine"] = "strands"
    if change == "source_hash":
        payload["sources"][0]["sha256"] = "b" * 64
    if change == "blob":
        payload["sources"][0]["blob_sha"] = "b" * 40
    if change == "source_ref":
        payload["sources"][0]["source_ref"] = "github://foreign/repo@blob/source"
    ref = archive(store, payload)
    if change == "hash":
        store.objects[ref["key"]] = b"{}"
    if change == "missing":
        store.objects.pop(ref["key"])
    if change == "cross_key":
        ref["key"] = ref["key"].replace("/incident/", "/other/")
    with pytest.raises((ValueError, KeyError)):
        read_operations_sources(store, "incident", ref)


def test_real_runner_publishes_original_ci_body_and_reference_and_reuses_without_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_context, "_ARTIFACT_ROOT", tmp_path)
    work = execution_context.ExecutionContext.create("incident")
    work.prepare()
    original = source()
    saved = copy.deepcopy(original)

    class WithCI(Scripted):
        def _run_single(self, prompt, **kwargs):
            if kwargs["profile"].endswith("operations"):
                save_ci_source(kwargs["execution_token"], original)
                write_once(
                    kwargs["execution_token"],
                    RESULT_FILES["operations"],
                    {
                        "title": "CI",
                        "summary": "recorded",
                        "findings": [
                            {
                                "statement": "Workflow read",
                                "status": "OBSERVED",
                                "evidence_refs": [original["source_ref"]],
                            }
                        ],
                        "recommendations": [],
                        "limitations": [],
                    },
                )
                return CodexResult(success=True, result="saved", raw_output="")
            return super()._run_single(prompt, **kwargs)

    parts = context()
    assert run(WithCI(), parts, work).success
    outcome = parts.outcomes["operations"]
    result = outcome["payload"]["result"]
    artifact = result["control_artifacts"][0]
    assert {key: artifact[key] for key in original} == saved
    assert artifact["base_revision"] == "blob:" + original["blob_sha"]
    assert artifact["source_kind"] == "read_control_configuration"
    assert artifact["source_phase"] == "current_ci"
    assert result["findings"][0]["evidence_refs"] == [artifact["source_ref"]]
    ref = next(x for x in outcome["payload"]["input_refs"] if x.get("kind") == "current_ci_sources")
    assert json.loads(parts.store._read_object(ref["key"]))["sources"] == [saved]
    before = copy.deepcopy(outcome)
    work.cleanup()
    resumed = execution_context.ExecutionContext.create("incident")
    resumed.prepare()
    runner = WithCI()
    assert run(runner, context(store=parts.store), resumed).success
    assert runner.calls == [] and parts.store.read_part("incident", "operations") == before
