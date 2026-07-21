import json
from threading import Event
from unittest.mock import Mock

import pytest

from cc_headless.services import artifact_watcher

CLAIM_TOKEN = "claim-token"


def _scan(artifact_dir, seen=None):
    seen = seen if seen is not None else {}
    artifact_watcher._scan_once(artifact_dir, "rca-1", None, seen, {}, CLAIM_TOKEN)
    return seen


@pytest.mark.parametrize(
    ("filename", "span_type"),
    [
        ("scoping.json", "SCOPING"),
        ("hypotheses.json", "HYPOTHESIS_GENERATION"),
        ("remediation.json", "REMEDIATION"),
        ("playbook.json", "PLAYBOOK"),
        ("report.md", "REPORT"),
        ("validation-3.json", "VALIDATION_LOOP"),
    ],
)
def test_scan_maps_supported_artifacts_to_spans(tmp_path, monkeypatch, filename, span_type):
    content = "# report" if filename.endswith(".md") else json.dumps({"summary": filename})
    (tmp_path / filename).write_text(content)
    write_span = Mock(return_value="span-id")
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    monkeypatch.setattr(artifact_watcher, "_save_hypotheses_to_ddb", Mock())
    monkeypatch.setattr(artifact_watcher, "_update_hypotheses_from_validation", Mock())

    seen = _scan(tmp_path)

    assert filename in seen
    assert write_span.call_args.args[2] == span_type
    if filename.startswith("validation-"):
        assert write_span.call_args.kwargs["loop_index"] == 3


def test_scan_ignores_unknown_files(tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("ignore me")
    write_span = Mock()
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)

    seen = _scan(tmp_path)

    assert seen == {}
    write_span.assert_not_called()


def test_malformed_json_records_failed_span_and_retries_repaired_version(tmp_path, monkeypatch):
    path = tmp_path / "scoping.json"
    path.write_text("{")
    write_span = Mock(return_value="span-id")
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    seen: dict[str, tuple[int, int]] = {}

    _scan(tmp_path, seen)
    assert "scoping.json" in seen
    assert write_span.call_args.args[3] == {"error": "Malformed JSON artifact"}

    path.write_text('{"summary": "repaired"}')
    _scan(tmp_path, seen)

    assert "scoping.json" in seen
    assert write_span.call_count == 2
    assert write_span.call_args.args[3]["summary"] == "repaired"


def test_malformed_hypotheses_never_create_hypothesis_items(tmp_path, monkeypatch):
    (tmp_path / "hypotheses.json").write_text("{not-json")
    save_hypotheses = Mock()
    write_span = Mock(return_value="span-id")
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    monkeypatch.setattr(artifact_watcher, "_save_hypotheses_to_ddb", save_hypotheses)

    _scan(tmp_path)

    assert write_span.call_args.args[3] == {"error": "Malformed JSON artifact"}
    save_hypotheses.assert_not_called()


def test_non_object_json_records_failed_span(tmp_path, monkeypatch):
    (tmp_path / "validation-1.json").write_text("[]")
    write_span = Mock(return_value="span-id")
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    monkeypatch.setattr(artifact_watcher, "_update_hypotheses_from_validation", Mock())

    _scan(tmp_path)

    assert write_span.call_args.args[3] == {"error": "JSON artifact must be an object"}


def test_changed_artifact_is_processed_again_after_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "scoping.json"
    path.write_text('{"summary": "first"}')
    write_span = Mock(return_value="span-id")
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    seen: dict[str, tuple[int, int]] = {}

    _scan(tmp_path, seen)
    path.write_text('{"summary": "regenerated"}')
    _scan(tmp_path, seen)

    assert write_span.call_count == 2
    assert write_span.call_args.args[3]["summary"] == "regenerated"


def test_unchanged_artifact_is_not_emitted_twice(tmp_path, monkeypatch):
    (tmp_path / "scoping.json").write_text('{"summary": "same"}')
    write_span = Mock(return_value="span-id")
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    seen: dict[str, tuple[int, int]] = {}

    _scan(tmp_path, seen)
    _scan(tmp_path, seen)

    write_span.assert_called_once()


def test_final_scan_processes_artifact_created_before_shutdown(tmp_path, monkeypatch):
    (tmp_path / "report.md").write_text("# complete")
    write_span = Mock(return_value="span-id")
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    stop = Event()
    stop.set()

    artifact_watcher._watch_loop(tmp_path, "rca-1", CLAIM_TOKEN, None, stop)

    write_span.assert_called_once()
    assert write_span.call_args.args[2] == "REPORT"


@pytest.mark.parametrize(
    ("filename", "expected_index"),
    [("validation-1.json", 1), ("validation-12.json", 12), ("validation-invalid.json", 0)],
)
def test_validation_loop_index_is_derived_from_filename(tmp_path, monkeypatch, filename, expected_index):
    (tmp_path / filename).write_text('{"summary": "validation"}')
    write_span = Mock(return_value="span-id")
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    monkeypatch.setattr(artifact_watcher, "_update_hypotheses_from_validation", Mock())

    _scan(tmp_path)

    assert write_span.call_args.kwargs["loop_index"] == expected_index


def test_validation_artifacts_are_processed_in_numeric_loop_order(tmp_path, monkeypatch):
    for loop_index in (10, 2):
        (tmp_path / f"validation-{loop_index}.json").write_text(
            json.dumps({"loop_index": loop_index, "summary": "validation"})
        )
    processed: list[int] = []
    monkeypatch.setattr(artifact_watcher, "_write_span", Mock(return_value="span-id"))
    monkeypatch.setattr(
        artifact_watcher,
        "validate_validation_artifacts",
        lambda base, *, through_loop_index: (
            base / f"validation-{through_loop_index}.json",
            {},
        ),
    )
    monkeypatch.setattr(
        artifact_watcher,
        "_update_hypotheses_from_validation",
        lambda ddb, rca_id, artifact, *, claim_token: processed.append(artifact["loop_index"]),
    )

    _scan(tmp_path)

    assert processed == [2, 10]


def test_invalid_validation_does_not_update_hypotheses(tmp_path, monkeypatch):
    (tmp_path / "validation-1.json").write_text('{"summary": "invalid"}')
    write_span = Mock(return_value="span-id")
    update_hypotheses = Mock()
    monkeypatch.setattr(artifact_watcher, "_write_span", write_span)
    monkeypatch.setattr(artifact_watcher, "_update_hypotheses_from_validation", update_hypotheses)
    monkeypatch.setattr(
        artifact_watcher,
        "validate_validation_artifacts",
        Mock(side_effect=artifact_watcher.ArtifactValidationError("unknown parent")),
    )

    _scan(tmp_path)

    assert write_span.call_args.args[3]["error"] == "unknown parent"
    update_hypotheses.assert_not_called()


def test_watcher_write_uses_transactional_current_claim_condition(monkeypatch):
    monkeypatch.setattr(artifact_watcher, "DYNAMODB_TABLE_NAME", "sessions")
    ddb = Mock()

    artifact_watcher._write_span(
        ddb,
        "rca-1",
        "REPORT",
        {"summary": "done"},
        claim_token=CLAIM_TOKEN,
    )

    items = ddb.transact_write_items.call_args.kwargs["TransactItems"]
    assert items[0]["ConditionCheck"]["ExpressionAttributeValues"][":claim"]["S"] == CLAIM_TOKEN
    assert items[1]["Put"]["Item"]["span_type"]["S"] == "REPORT"


def test_remediation_span_stores_only_bounded_dashboard_metadata(monkeypatch):
    monkeypatch.setattr(artifact_watcher, "DYNAMODB_TABLE_NAME", "sessions")
    ddb = Mock()
    artifact = {
        "status": "BLOCKED",
        "fault_type": "unsupported",
        "endpoint_path": None,
        "verification": {
            "status": "PENDING",
            "reason": "x" * 600,
            "raw_response": {"must": "not be copied"},
        },
        "confirmed_hypothesis_ids": ["hypothesis-1"],
        "error": "must not be copied to metadata",
        "raw_response": {"large": "payload"},
    }

    artifact_watcher._write_span(
        ddb,
        "rca-1",
        "REMEDIATION",
        artifact,
        claim_token=CLAIM_TOKEN,
    )

    item = ddb.transact_write_items.call_args.kwargs["TransactItems"][1]["Put"]["Item"]
    metadata = item["metadata"]["M"]
    assert set(metadata) == {"status", "fault_type", "endpoint_path", "verification"}
    assert metadata["status"] == {"S": "BLOCKED"}
    assert metadata["fault_type"] == {"S": "unsupported"}
    assert metadata["endpoint_path"] == {"S": ""}
    assert metadata["verification"]["M"]["status"] == {"S": "PENDING"}
    assert metadata["verification"]["M"]["reason"] == {"S": "x" * 500}
    assert set(metadata["verification"]["M"]) == {"status", "reason"}


def test_remediation_span_omits_unrecognized_metadata_status(monkeypatch):
    monkeypatch.setattr(artifact_watcher, "DYNAMODB_TABLE_NAME", "sessions")
    ddb = Mock()

    artifact_watcher._write_span(
        ddb,
        "rca-1",
        "REMEDIATION",
        {
            "status": "NOT_ATTEMPTED",
            "fault_type": "db-leak",
            "endpoint_path": None,
            "verification": {"status": "PENDING", "reason": "not run"},
        },
        claim_token=CLAIM_TOKEN,
    )

    item = ddb.transact_write_items.call_args.kwargs["TransactItems"][1]["Put"]["Item"]
    assert "status" not in item["metadata"]["M"]
