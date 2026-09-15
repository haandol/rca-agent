"""Real Strands adapter results keep revision audits without appraising the same logical playbook twice."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from rca_agent.services.playbook_gen import run_playbook_generation
from tests import test_playbook_library as library
from tests.test_playbook_comparison import _draft
from tests.test_playbook_gen import _make_appraisal, _make_existing, _make_report

storage = library.storage


def published_pointers(storage):
    """Publish through the actual store, leaving a simulated old revision pointer in the search response."""
    ddb, vectors, store = storage
    value = _make_existing(playbook_id="pb-1", rca_id="rca-1").model_dump(mode="json")
    library.source(ddb, value)
    assert library.publish(store, value)
    head = store._library.head("pb-1")
    stale = library.hit(head, distance=0.05)
    stale["key"] = "pb-1@analysis:previous"
    stale["metadata"]["library_revision"] = "analysis:previous"
    return vectors, store, {"stale": stale, "current": library.hit(head, distance=0.1)}, head


def generate(store):
    """Use the same current draft and a deterministic no-change appraisal for control and mixed results."""
    agent = MagicMock(
        side_effect=[
            MagicMock(structured_output=_draft()),
            MagicMock(structured_output=_make_appraisal()),
        ]
    )
    return run_playbook_generation(_make_report(), agent, playbook_store=store), agent


@pytest.mark.parametrize(
    "order",
    [
        ["stale", "current"],
        ["current", "stale"],
        ["stale", "stale", "current"],
        ["current", "stale", "stale"],
        ["current", "current", "stale"],
        ["stale", "current", "current"],
    ],
)
def test_old_current_and_repeated_pointers_match_current_only_control(storage, order):
    """Only the current original is appraised; stale and repeated rows remain visible without model judgments."""
    vectors, store, pointers, head = published_pointers(storage)
    vectors.query_vectors.return_value = {"vectors": [deepcopy(pointers["current"])]}
    control, _ = generate(store)
    assert control.comparison["status"] == "NO_CHANGE"
    vectors.query_vectors.return_value = {"vectors": [deepcopy(pointers[key]) for key in order]}
    result, agent = generate(store)
    wire = result.comparison
    assert wire["status"] == "NO_CHANGE"
    assert result.playbook_id == wire["selected_playbook_id"] == "pb-1"
    assert wire["baseline"] == control.comparison["baseline"]
    assert result.execution_steps == control.execution_steps
    assert agent.call_count == 2  # One current draft plus one logical-playbook appraisal.
    assert len(wire["inputs"]["candidate_comparisons"]) == 1
    assert len(wire["candidates"]) == len(order)
    available = [candidate for candidate in wire["candidates"] if candidate["availability"] == "AVAILABLE"]
    assert len(available) == 1
    assert available[0]["revision"] == head["revision"]
    assert available[0]["applicable"] is True
    for candidate in wire["candidates"]:
        if candidate["availability"] == "UNAVAILABLE":
            assert candidate["applicable"] is None
            assert candidate["rationale"]
            assert "evidence" not in candidate
            if candidate["revision"] == "analysis:previous":
                assert "published detail unavailable" in candidate["rationale"]
    assert [item["revision"] for item in wire["used_references"] if item["role"] == "knowledge-reuse"] == [
        head["revision"]
    ]


@pytest.mark.parametrize("count", [1, 2, 3])
def test_only_stale_pointers_remain_search_failed_and_fully_auditable(storage, count):
    """Repeated unavailable pointers never become no-match or generation references."""
    vectors, store, pointers, _ = published_pointers(storage)
    vectors.query_vectors.return_value = {"vectors": [deepcopy(pointers["stale"]) for _ in range(count)]}
    result, agent = generate(store)
    assert result.comparison["status"] == "SEARCH_FAILED"
    assert result.playbook_id != "pb-1"
    assert len(result.comparison["candidates"]) == count
    assert all(
        candidate["availability"] == "UNAVAILABLE" and candidate["rationale"] and candidate["applicable"] is None
        for candidate in result.comparison["candidates"]
    )
    assert agent.call_count == 1
