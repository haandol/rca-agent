"""Concurrent public MCP first reads: delayed S3/cache writes, same document and both documents."""

import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastmcp import Client

from headless_codex import retrospective_mcp_server as server
from headless_codex.services import retrospective_reader as reader


@pytest.mark.parametrize("documents", [(["evidence"] * 6), (["evidence", "approved_playbook"] * 3)])
def test_parallel_first_reads_preserve_complete_cache_and_both_receipts(retrospective_sources, monkeypatch, documents):
    work = retrospective_sources.prepare()
    original_get = retrospective_sources.client.get_object

    def delayed_get(**kwargs):
        time.sleep(0.03)
        return original_get(**kwargs)

    monkeypatch.setattr(retrospective_sources.client, "get_object", delayed_get)
    original_state = reader._state

    def delayed_state(*args):
        state = original_state(*args)
        time.sleep(0.02)
        return state

    monkeypatch.setattr(reader, "_state", delayed_state)
    original_fdopen = reader.os.fdopen

    class SlowOutput:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, data):
            self.file.write(data[:1])
            self.file.flush()
            # Even a reader outside our lock must never see a published partial JSON cache.
            for cache in work.path.glob("retrospective-*-cache.json"):
                json.loads(cache.read_bytes())
            time.sleep(0.04)
            return 1 + self.file.write(data[1:])

    def fdopen(fd, mode="r", *args, **kwargs):
        file = original_fdopen(fd, mode, *args, **kwargs)
        return SlowOutput(file) if mode == "wb" else file

    monkeypatch.setattr(reader.os, "fdopen", fdopen)

    async def run():
        async with Client(server.mcp) as client:

            async def read(document):
                reply = await client.call_tool("read_retrospective_document", {"document": document})
                return json.loads(reply.content[0].text)

            return await asyncio.gather(*(read(document) for document in documents))

    replies = asyncio.run(run())
    assert all(r["ok"] for r in replies), replies
    _, fingerprint = reader._reference(work.token, work.execution_id)
    state = reader._state(work.token, fingerprint)
    assert set(state["documents_read"]) == set(documents)
    assert not state["failed"]
    for document in set(documents):
        ref, _ = reader._reference(work.token, work.execution_id)
        raw = retrospective_sources.objects[ref["documents"][document]]
        assert (work.path / f"retrospective-{document}-cache.json").read_bytes() == raw
        assert state["document_sha256"][document] == hashlib.sha256(raw).hexdigest()
    if set(documents) == {"evidence", "approved_playbook"}:
        reader.require_successful_reads(work.token, work.execution_id)
    else:
        with pytest.raises(ValueError):
            reader.require_successful_reads(work.token, work.execution_id)


def test_parallel_source_failure_cannot_be_overwritten_by_success(retrospective_sources, monkeypatch):
    work = retrospective_sources.prepare()
    ref, _ = reader._reference(work.token, work.execution_id)
    retrospective_sources.objects[ref["documents"]["approved_playbook"]] = b"not JSON"
    start = Barrier(2)

    def read(document):
        start.wait()
        return reader.read_document(work.token, work.execution_id, document)

    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(read, ["evidence", "approved_playbook"]))
    assert any(not reply["ok"] for reply in replies)
    with pytest.raises(ValueError):
        reader.require_successful_reads(work.token, work.execution_id)
    assert not json.loads(server.save_playbook_update("{}", "failed source"))["ok"]


def _process_read(root, token, execution_id, document, objects, barrier, queue):
    import io
    from pathlib import Path
    from types import SimpleNamespace

    from headless_codex.services import execution_workspace

    execution_workspace._WORKSPACE_ROOT = Path(root)
    reader.S3_EVIDENCE_BUCKET = "offline-evidence"
    gets = []

    def get_object(**kwargs):
        gets.append(kwargs["Key"])
        time.sleep(0.03)
        return {"Body": io.BytesIO(objects[kwargs["Key"]])}

    reader._s3_client = lambda: SimpleNamespace(get_object=get_object)
    original_state = reader._state

    def state(*args):
        value = original_state(*args)
        time.sleep(0.03)
        return value

    reader._state = state
    barrier.wait(timeout=15)
    reply = reader.read_document(token, execution_id, document)
    queue.put({"reply": reply, "gets": gets})


@pytest.mark.parametrize("documents", [("evidence", "evidence"), ("evidence", "approved_playbook")])
def test_separate_mcp_processes_share_token_lock(retrospective_sources, documents):
    import multiprocessing

    work = retrospective_sources.prepare()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    barrier = context.Barrier(2)
    processes = [
        context.Process(
            target=_process_read,
            args=(
                str(work.path.parent),
                work.token,
                work.execution_id,
                document,
                retrospective_sources.objects,
                barrier,
                queue,
            ),
        )
        for document in documents
    ]
    for process in processes:
        process.start()
    try:
        results = [queue.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        assert all(result["reply"]["ok"] for result in results), results
        # Concurrent same-document cold reads fetch only once and reuse exact completed bytes.
        assert sum(len(result["gets"]) for result in results) == len(set(documents))
        _, fingerprint = reader._reference(work.token, work.execution_id)
        state = reader._state(work.token, fingerprint)
        assert set(state["documents_read"]) == set(documents)
        assert not state["failed"]
        if len(set(documents)) == 2:
            reader.require_successful_reads(work.token, work.execution_id)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        queue.close()


def test_validator_waits_for_inflight_receipt_update(retrospective_sources, monkeypatch):
    from threading import Event

    work = retrospective_sources.prepare()
    assert reader.read_document(work.token, work.execution_id, "evidence")["ok"]
    writing = Event()
    release = Event()
    original_write = reader.write_observation_json

    def delayed_write(path, record):
        if path.name == "retrospective-read-state.json":
            writing.set()
            assert release.wait(5)
        return original_write(path, record)

    monkeypatch.setattr(reader, "write_observation_json", delayed_write)
    with ThreadPoolExecutor(2) as pool:
        read = pool.submit(reader.read_document, work.token, work.execution_id, "approved_playbook")
        assert writing.wait(5)
        validate = pool.submit(reader.require_successful_reads, work.token, work.execution_id)
        time.sleep(0.05)
        assert not validate.done()
        release.set()
        assert read.result(timeout=5)["ok"]
        validate.result(timeout=5)
