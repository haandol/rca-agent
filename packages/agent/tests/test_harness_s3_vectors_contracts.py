from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store import (
    S3VectorsPlaybookStore,
)
from rca_agent.adapters.secondary.report.s3_report_store import S3ReportStore


@pytest.fixture()
def embedding():
    adapter = MagicMock()
    adapter.embed_query.return_value = [0.1, 0.2, 0.3]
    return adapter


@pytest.mark.parametrize(
    ("store_factory", "module", "index_name"),
    [
        (
            lambda client, embedding: S3ReportStore(
                s3_vectors_client=client,
                embedding=embedding,
            ),
            "rca_agent.adapters.secondary.report.s3_report_store",
            "report-contract",
        ),
        (
            lambda client, embedding: S3VectorsPlaybookStore(
                s3_vectors_client=client,
                embedding=embedding,
            ),
            "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store",
            "playbook-contract",
        ),
    ],
)
def test_query_uses_float32_vector_bucket_index_and_top_k(
    store_factory,
    module,
    index_name,
    embedding,
):
    client = MagicMock()
    client.query_vectors.return_value = {"vectors": []}
    store = store_factory(client, embedding)

    with (
        patch(f"{module}.S3_VECTOR_BUCKET_NAME", "vector-bucket"),
        patch(
            f"{module}.S3_VECTOR_REPORT_INDEX" if "report" in module else f"{module}.S3_VECTOR_PLAYBOOK_INDEX",
            index_name,
        ),
        patch(f"{module}.REPORT_TOP_K" if "report" in module else f"{module}.PLAYBOOK_TOP_K", 7),
    ):
        assert store.search_similar("database saturation") == []

    embedding.embed_query.assert_called_once_with("database saturation")
    kwargs = client.query_vectors.call_args.kwargs
    assert kwargs["vectorBucketName"] == "vector-bucket"
    assert kwargs["indexName"] == index_name
    assert kwargs["queryVector"] == {"float32": [0.1, 0.2, 0.3]}
    assert kwargs["topK"] == 7


@pytest.mark.parametrize(
    ("store_factory", "module"),
    [
        (
            lambda client, embedding: S3ReportStore(
                s3_vectors_client=client,
                embedding=embedding,
            ),
            "rca_agent.adapters.secondary.report.s3_report_store",
        ),
        (
            lambda client, embedding: S3VectorsPlaybookStore(
                s3_vectors_client=client,
                embedding=embedding,
            ),
            "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store",
        ),
    ],
)
def test_query_explicitly_requests_metadata(store_factory, module, embedding):
    client = MagicMock()
    client.query_vectors.return_value = {"vectors": []}
    store = store_factory(client, embedding)

    with patch(f"{module}.S3_VECTOR_BUCKET_NAME", "vector-bucket"):
        store.search_similar("database saturation")

    assert client.query_vectors.call_args.kwargs["returnMetadata"] is True


@pytest.mark.parametrize(
    ("store_factory", "module", "threshold_name"),
    [
        (
            lambda client, embedding: S3ReportStore(
                s3_vectors_client=client,
                embedding=embedding,
            ),
            "rca_agent.adapters.secondary.report.s3_report_store",
            "REPORT_SIMILARITY_THRESHOLD",
        ),
        (
            lambda client, embedding: S3VectorsPlaybookStore(
                s3_vectors_client=client,
                embedding=embedding,
            ),
            "rca_agent.adapters.secondary.playbook.s3_vectors_playbook_store",
            "PLAYBOOK_SIMILARITY_THRESHOLD",
        ),
    ],
)
def test_cosine_distance_is_converted_to_similarity(
    store_factory,
    module,
    threshold_name,
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

    with (
        patch(f"{module}.S3_VECTOR_BUCKET_NAME", "vector-bucket"),
        patch(f"{module}.{threshold_name}", 0.7),
    ):
        matches = store.search_similar("database saturation")

    assert [match.similarity for match in matches] == pytest.approx([0.9])
    assert [getattr(match, "rca_id", getattr(match, "playbook_id", None)) for match in matches] == ["near"]


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
        assert playbook_store.search_similar("query") == []

    embedding.embed_query.assert_not_called()
    client.query_vectors.assert_not_called()
