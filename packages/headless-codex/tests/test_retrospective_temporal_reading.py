"""Actual Case12 temporal evidence vs an explicitly synthetic contradictory last alarm poll."""

import copy
import json
from pathlib import Path

import pytest

from headless_codex import retrospective_mcp_server as server
from headless_codex.services import retrospective_reader as reader

FIXTURE = Path(__file__).parent / "fixtures/retrospective-temporal-case12.json"


@pytest.mark.parametrize("last_state", ["OK", "ALARM"])
def test_frozen_alarm_input_is_distinct_from_timestamped_last_poll(retrospective_sources, last_state):
    evidence = copy.deepcopy(json.loads(FIXTURE.read_text())["evidence"])
    if last_state == "ALARM":
        # Synthetic contradiction, not an actual AWS observation or rewrite of the source fixture.
        poll = evidence["metric_wait_records"][2]
        payload = json.loads(poll["response"]["stdout"])
        payload["MetricAlarms"][0]["StateValue"] = "ALARM"
        poll["response"]["stdout"] = json.dumps(payload)
    work = retrospective_sources.prepare(evidence["rca_id"], evidence["execution_id"], evidence)
    raw_before = dict(retrospective_sources.objects)

    def read(pointer, **kwargs):
        result = json.loads(server.read_retrospective_document("evidence", pointer, **kwargs))
        assert result["ok"]
        assert len(json.dumps(result, ensure_ascii=False)) <= 12000
        return result

    index = read("/metric_wait_records")
    polls = [item for item in index["items"] if item["labels"].get("role") == "alarms"]
    assert [item["labels"]["phase"] for item in polls] == ["poll", "poll"]
    for item in polls:
        source = evidence["metric_wait_records"][item["key"]]
        assert item["labels"]["observed_at"] == source["observed_at"]
        assert item["labels"]["recorded_at"] == source["recorded_at"]
        assert item["index_only"] is True
    last = max(polls, key=lambda item: item["labels"]["observed_at"])
    result = read(last["pointer"] + "/response/stdout")
    assert result["complete"]
    assert json.loads(result["text"])["MetricAlarms"][0]["StateValue"] == last_state
    assert read("/metric_wait_records/3/binding/alarms/failures/StateValue")["text"] == "ALARM"
    assert read("/metric_wait_records/3/status")["text"] == "HEALTHY"
    assert read("/final_state")["text"] == "RESOLVED"
    assert retrospective_sources.objects == raw_before
    assert (work.path / "retrospective-evidence-cache.json").read_bytes() in raw_before.values()


def test_temporal_labels_remain_bounded_without_truncating_or_inventing_times(retrospective_sources):
    records = [
        {
            "role": "alarms",
            "phase": "poll",
            "observed_at": "2026-09-16T06:39:08.551063+00:00",
            "recorded_at": "2026-09-16T06:39:08.551137+00:00",
            "started_at": "2026-09-16T06:39:08+00:00",
            "ended_at": "2026-09-16T06:39:09+00:00",
            "status": "observed",
        }
        for _ in range(100)
    ]
    records.append({"phase": "poll"})
    work = retrospective_sources.prepare(
        evidence={
            "rca_id": "rca",
            "execution_id": "exec",
            "playbook_id": "pb",
            "final_state": "RESOLVED",
            "resolution_confirmed": True,
            "records": records,
        }
    )
    offset = 0
    seen = []
    while True:
        result = reader.read_document(
            work.token, work.execution_id, "evidence", "/records", offset=offset, max_items=50
        )
        assert result["ok"] and len(json.dumps(result, ensure_ascii=False)) <= 12000
        for item in result["items"]:
            assert item["labels"] == records[item["key"]]
        seen.extend(item["key"] for item in result["items"])
        if result["complete"]:
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    assert seen == list(range(101))
