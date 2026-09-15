"""Real adapter results preserve pointer audits while comparing one current body per logical playbook."""

from copy import deepcopy

import pytest
import test_playbook_comparison as comparisons
import test_playbook_library as library

incident = comparisons.incident
storage = library.storage


def published_pointers(incident, storage):
    """Publish an actual current source and simulate best-effort cleanup leaving an older vector."""
    ddb, vectors, store = storage
    value = comparisons.comparison.historical_snapshot(incident.existing)
    value.update(playbook_id="pb-1", rca_id="rca-1")
    library.source(ddb, value)
    assert library.publish(store, value)
    head = store._library.head("pb-1")
    stale = library.hit(head, distance=0.05)
    stale["key"] = "pb-1@analysis:previous"
    stale["metadata"]["library_revision"] = "analysis:previous"
    incident.store = store
    return vectors, {"stale": stale, "current": library.hit(head, distance=0.1)}, head


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
def test_old_current_and_repeated_pointers_match_current_only_control(incident, storage, order):
    """Ordering and duplicate pointers cannot hide the current body or assign its judgment to a stale row."""
    vectors, pointers, head = published_pointers(incident, storage)
    vectors.query_vectors.return_value = {"vectors": [deepcopy(pointers["current"])]}
    control = comparisons.run_comparison(incident, rca_id="current-incident")
    assert control["comparison"]["status"] == "NO_CHANGE"
    incident.runner.compare_playbooks.reset_mock()

    vectors.query_vectors.return_value = {"vectors": [deepcopy(pointers[key]) for key in order]}
    result = comparisons.run_comparison(incident, rca_id="current-incident")

    wire = result["comparison"]
    assert wire["status"] == "NO_CHANGE"
    assert wire["selected_playbook_id"] == result["playbook_id"] == "pb-1"
    assert wire["baseline"] == control["comparison"]["baseline"]
    assert result["execution_steps"] == control["execution_steps"] == incident.current["execution_steps"]
    assert incident.runner.compare_playbooks.call_count == 1
    payload = incident.runner.compare_playbooks.call_args.args[0]
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["revision"] == head["revision"]
    assert len(wire["candidates"]) == len(order)
    assert [candidate["revision"] for candidate in wire["candidates"]] == [
        pointers[key]["metadata"]["library_revision"] for key in order
    ]
    available = [candidate for candidate in wire["candidates"] if candidate["availability"] == "AVAILABLE"]
    assert len(available) == 1
    assert available[0]["revision"] == head["revision"]
    assert available[0]["applicable"] is True
    for candidate in wire["candidates"]:
        if candidate["availability"] == "UNAVAILABLE":
            assert candidate["applicable"] is None
            assert candidate["rationale"]
            assert "Observed lock owner" not in candidate["rationale"]
            if candidate["revision"] == "analysis:previous":
                assert "published detail unavailable" in candidate["rationale"]
    assert [item["revision"] for item in wire["used_references"] if item["role"] == "knowledge-reuse"] == [
        head["revision"]
    ]


@pytest.mark.parametrize("count", [1, 2, 3])
def test_only_stale_pointers_remain_search_failed_and_fully_auditable(incident, storage, count):
    """Unavailable-only responses retain every pointer and never invent a usable model input."""
    vectors, pointers, _ = published_pointers(incident, storage)
    vectors.query_vectors.return_value = {"vectors": [deepcopy(pointers["stale"]) for _ in range(count)]}
    result = comparisons.run_comparison(incident, rca_id="current-incident")
    assert result["comparison"]["status"] == "SEARCH_FAILED"
    assert result["playbook_id"] == incident.current["playbook_id"]
    assert len(result["comparison"]["candidates"]) == count
    assert all(
        candidate["availability"] == "UNAVAILABLE" and candidate["rationale"] and candidate["applicable"] is None
        for candidate in result["comparison"]["candidates"]
    )
    incident.runner.compare_playbooks.assert_not_called()
