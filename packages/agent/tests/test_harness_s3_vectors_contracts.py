from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store import (
    S3VectorsPlaybookStore,
)
from rca_agent.adapters.secondary.report.s3_report_store import S3ReportStore
from rca_agent.ports.dto.models import AlarmPayload, AlarmTrigger, Playbook, RcaReport, ScopingResult
from rca_agent.services.playbook_gen import _build_embed_key as _build_playbook_embed_key
from rca_agent.services.scoping import build_report_query


@pytest.fixture()
def embedding():
    adapter = MagicMock()
    adapter.embed_query.return_value = [0.1, 0.2, 0.3]
    adapter.embed_document.return_value = [0.1, 0.2, 0.3]
    return adapter


_REPORT_MODULE = "rca_agent.adapters.secondary.report.s3_report_store"
_PLAYBOOK_MODULE = "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store"

# Both indexes must honour the same query contract, but they take their cutoff
# differently: the report index owns its threshold, while playbook callers pass
# one because retrieval and merging need different strictness.
_STORE_CASES = [
    pytest.param(
        lambda client, embedding: S3ReportStore(s3_vectors_client=client, embedding=embedding),
        _REPORT_MODULE,
        lambda store, query: store.search_similar(query),
        "S3_VECTOR_REPORT_INDEX",
        "REPORT_TOP_K",
        "REPORT_SIMILARITY_THRESHOLD",
        "rca_id",
        id="report",
    ),
    pytest.param(
        lambda client, embedding: S3VectorsPlaybookStore(s3_vectors_client=client, embedding=embedding),
        _PLAYBOOK_MODULE,
        lambda store, query: store.search_similar(query, threshold=0.7),
        "S3_VECTOR_PLAYBOOK_INDEX",
        "PLAYBOOK_TOP_K",
        None,
        "playbook_id",
        id="playbook",
    ),
]

_STORE_CASE_FIELDS = (
    "store_factory",
    "module",
    "search",
    "index_setting",
    "top_k_setting",
    "threshold_setting",
    "key_field",
)


@pytest.mark.parametrize(_STORE_CASE_FIELDS, _STORE_CASES)
def test_query_uses_float32_vector_bucket_index_and_top_k(
    store_factory,
    module,
    search,
    index_setting,
    top_k_setting,
    threshold_setting,  # noqa: ARG001
    key_field,  # noqa: ARG001
    embedding,
):
    client = MagicMock()
    client.query_vectors.return_value = {"vectors": []}
    store = store_factory(client, embedding)

    with (
        patch(f"{module}.S3_VECTOR_BUCKET_NAME", "vector-bucket"),
        patch(f"{module}.{index_setting}", "index-contract"),
        patch(f"{module}.{top_k_setting}", 7),
    ):
        assert search(store, "database saturation") == []

    embedding.embed_query.assert_called_once_with("database saturation")
    kwargs = client.query_vectors.call_args.kwargs
    assert kwargs["vectorBucketName"] == "vector-bucket"
    assert kwargs["indexName"] == "index-contract"
    assert kwargs["queryVector"] == {"float32": [0.1, 0.2, 0.3]}
    assert kwargs["topK"] == 7


@pytest.mark.parametrize(_STORE_CASE_FIELDS, _STORE_CASES)
def test_query_explicitly_requests_metadata(
    store_factory,
    module,
    search,
    index_setting,  # noqa: ARG001
    top_k_setting,  # noqa: ARG001
    threshold_setting,  # noqa: ARG001
    key_field,  # noqa: ARG001
    embedding,
):
    client = MagicMock()
    client.query_vectors.return_value = {"vectors": []}
    store = store_factory(client, embedding)

    with patch(f"{module}.S3_VECTOR_BUCKET_NAME", "vector-bucket"):
        search(store, "database saturation")

    assert client.query_vectors.call_args.kwargs["returnMetadata"] is True


@pytest.mark.parametrize(_STORE_CASE_FIELDS, _STORE_CASES)
def test_cosine_distance_is_converted_to_similarity(
    store_factory,
    module,
    search,
    index_setting,  # noqa: ARG001
    top_k_setting,  # noqa: ARG001
    threshold_setting,
    key_field,
    embedding,
):
    client = MagicMock()
    client.query_vectors.return_value = {
        "vectors": [
            {
                "key": "near",
                "distance": 0.1,
                "metadata": {
                    "incident_summary": "near report",
                    "failure_type": "near playbook",
                },
            },
            {
                "key": "far",
                "distance": 0.8,
                "metadata": {
                    "incident_summary": "far report",
                    "failure_type": "far playbook",
                },
            },
        ]
    }
    store = store_factory(client, embedding)

    # Each case searches at a 0.7 cutoff, so only the near hit (distance 0.1 →
    # similarity 0.9) survives; treating distance as similarity would invert this.
    with ExitStack() as patches:
        patches.enter_context(patch(f"{module}.S3_VECTOR_BUCKET_NAME", "vector-bucket"))
        if threshold_setting:
            patches.enter_context(patch(f"{module}.{threshold_setting}", 0.7))
        matches = search(store, "database saturation")

    assert [match.similarity for match in matches] == pytest.approx([0.9])
    assert [getattr(match, key_field) for match in matches] == ["near"]


def test_search_is_disabled_without_vector_bucket(embedding):
    client = MagicMock()
    report_store = S3ReportStore(s3_vectors_client=client, embedding=embedding)
    playbook_store = S3VectorsPlaybookStore(s3_vectors_client=client, embedding=embedding)

    with (
        patch("rca_agent.adapters.secondary.report.s3_report_store.S3_VECTOR_BUCKET_NAME", ""),
        patch(
            "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store.S3_VECTOR_BUCKET_NAME",
            "",
        ),
    ):
        assert report_store.search_similar("query") == []
        assert playbook_store.search_similar("query", threshold=0.7) == []

    embedding.embed_query.assert_not_called()
    client.query_vectors.assert_not_called()


def _document_embed_text(store, module, save, embedding) -> str:
    """Return the text a store embeds when writing to its index."""
    embedding.embed_document.reset_mock()
    with patch(f"{module}.S3_VECTOR_BUCKET_NAME", "vector-bucket"):
        save(store)
    return embedding.embed_document.call_args.args[0]


@pytest.mark.parametrize("metric_name", ["CPUUtilization", ""])
def test_index_writer_and_searcher_render_the_same_embed_text(metric_name, embedding):
    """A stored vector is only findable if both sides render text identically.

    The metric is absent whenever an alarm carries no trigger, so the empty case
    has to match too — a dangling ``메트릭:`` label on one side is silent drift.
    """
    alarm = AlarmPayload(
        alarm_name="Memory leak in worker",
        new_state_reason="CPU spike on web-service",
        trigger=AlarmTrigger(metric_name=metric_name, namespace="AWS/ECS") if metric_name else None,
    )
    scoping = ScopingResult(alarm_summary="CPU spike", raw_alarm=alarm)
    report = RcaReport(
        rca_id="rca-1",
        incident_summary=alarm.new_state_reason,
        root_cause=alarm.alarm_name,
        confidence_score=0.9,
    )

    report_written = _document_embed_text(
        S3ReportStore(s3_vectors_client=MagicMock(), embedding=embedding),
        "rca_agent.adapters.secondary.report.s3_report_store",
        lambda store: store.save_vectors(report, scoping_result=scoping),
        embedding,
    )
    assert report_written == build_report_query(alarm)

    playbook_written = _document_embed_text(
        S3VectorsPlaybookStore(s3_vectors_client=MagicMock(), embedding=embedding),
        "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store",
        lambda store: store.save(
            Playbook(
                playbook_id="p-1",
                failure_type=report.root_cause,
                symptom_pattern=report.incident_summary,
            ),
            scoping_result=scoping,
        ),
        embedding,
    )
    assert playbook_written == _build_playbook_embed_key(report, scoping)

    # Both indexes share one embedding space, so the two writers must agree too.
    assert report_written == playbook_written
    assert ("메트릭" in report_written) is bool(metric_name)
