"""Complete high-volume source/error input pages without relaxing the observation deadline."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from headless_codex.services.recovery_evidence import prepare_recovery_evidence

pytest_plugins = ["test_recovery_evidence"]


@pytest.mark.parametrize("mode", ["complete", "repeated_token", "budget"])
def test_eight_page_input_and_error_reads_preserve_completeness_guards(compatible, monkeypatch, mode):
    """Reproduce629 events plus a terminal empty page; loop/budget failures stay unavailable."""
    from headless_codex.services import recovery_observation as module

    reader, alarm, _, _, _, logs, _ = compatible
    original = deepcopy(logs.filter_log_events.return_value["events"])
    templates = {json.loads(e["message"])["event"]: e for e in original}
    calls = []
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def query(**kwargs):
        """Return deterministic fixture pages scoped to the same task and original requested window."""
        kind = kwargs["filterPattern"].split('"')[1]
        page = int(kwargs.get("nextToken", "0").rsplit(":", 1)[-1])
        calls.append((kind, page, kwargs["startTime"], kwargs["endTime"]))
        assert kwargs["logStreamNames"] == ["ecs/app/bad-task"]
        if kind not in {"db_write_error", "input_contract_observed"}:
            return {"events": [e for e in original if json.loads(e["message"])["event"] == kind]}
        if mode == "budget" and page == 3:
            clock[0] = 599.0
        events = []
        for number in range(page * 100, min((page + 1) * 100, 629)):
            event = deepcopy(templates[kind])
            event["eventId"] = f"{kind}-{number}"
            message = json.loads(event["message"])
            message["request_id"] = f"{number:032x}"
            event["message"] = json.dumps(message)
            events.append(event)
        if page == 7:
            return {"events": []}
        token = f"{kind}:{page if mode == 'repeated_token' and page == 3 else page + 1}"
        return {"events": events, "nextToken": token}

    logs.filter_log_events.side_effect = query
    observed = reader.observe(alarm, timeout_seconds=600)
    result = prepare_recovery_evidence(SimpleNamespace(raw_alarm=alarm, incident_observations=observed))
    window = observed.current["log_window"]
    assert len({(start, end) for _, _, start, end in calls}) == 1
    if mode == "complete":
        for kind in ("db_write_error", "input_contract_observed"):
            assert window["pages"][kind] == 8
            assert window["coverage"][kind] == "complete"
            assert sum(e["message"]["event"] == kind for e in observed.current["observations"]) == 629
        assert result["verification"]["status"] == "VERIFIED"
        current = [w for w in result["verification"]["witnesses"] if w["phase"] == "current"]
        assert len(current) == 629
        assert any("#db_write_error-628" in w["outcome_ref"] for w in current)
    else:
        assert result["verification"]["status"] == "UNAVAILABLE"
        expected = "budget_exhausted" if mode == "budget" else "pagination_incomplete"
        assert expected in window["coverage"].values()
        assert observed.current["observations"]
        assert all(page <= 3 for _, page, _, _ in calls)
