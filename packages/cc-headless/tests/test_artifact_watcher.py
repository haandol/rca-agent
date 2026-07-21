import json
from threading import Event
from unittest.mock import Mock

import pytest

from cc_headless.services import artifact_watcher


def _scan(artifact_dir, seen=None):
    seen = seen if seen is not None else {}
    artifact_watcher._scan_once(artifact_dir, "rca-1", None, seen, {})
    return seen


@pytest.mark.parametrize(
    ("filename", "span_type"),
    [
        ("scoping.json", "SCOPING"),
        ("hypotheses.json", "HYPOTHESIS_GENERATION"),
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

    artifact_watcher._watch_loop(tmp_path, "rca-1", None, stop)

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
