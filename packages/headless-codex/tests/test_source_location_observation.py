"""Actual reader paths retain only validated optional source locator metadata."""

import json

pytest_plugins = ["recovery_fixture", "test_recovery_evidence"]


def test_native_reader_preserves_safe_baseline_and_current_locators(compatible):
    reader, alarm, baseline, publish, _, logs, _ = compatible

    def add(message):
        message["source_locations"] = {
            "revision/write.py": {
                "repository": "o/r",
                "commit": "a" * 40,
                "path": "packages/healthcare-sensor-app/demo/revisions/v2/revision/write.py",
                "sha256": message["files"]["revision/write.py"],
                "verification": "declared",
            }
        }
        return message

    add(baseline["observations"][0]["message"])
    for event in logs.filter_log_events.return_value["events"]:
        value = json.loads(event["message"])
        if value["event"] == "source_manifest":
            event["message"] = json.dumps(add(value))
    publish()
    result = reader.observe(alarm, timeout_seconds=90)
    for observations in (result.baseline["observations"], result.current["observations"]):
        source = next(row["message"] for row in observations if row["message"]["event"] == "source_manifest")
        assert source["source_locations"]["revision/write.py"]["verification"] == "declared"
        assert source["source_locations"]["revision/write.py"]["sha256"] == source["files"]["revision/write.py"]
