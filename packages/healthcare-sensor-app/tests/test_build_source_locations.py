"""Bind checked-in source bytes without elevating declared metadata to Git evidence."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from tests.test_write_revision import PACKAGE, builder_module


def observe(destination, tmp_path):
    """Verify installed files in isolation; runtime source flags must not replace metadata."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from test_service.revision.manifest import source_manifest; "
            "print(json.dumps(source_manifest()))",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(destination), "SOURCE_COMMIT": "runtime-must-not-win"},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("revision", ["v1", "v2"])
def test_exact_checked_in_bytes_and_declared_location_survive_build(revision, tmp_path):
    builder = builder_module()
    snapshot = tmp_path / "snapshot"
    captured = builder.capture_source_snapshot(snapshot)
    assert "demo/revisions/v2/revision/write.py" in captured["files"]
    destination = tmp_path / "installed"
    manifest = builder.compile_revision(
        revision, destination, source_package=snapshot, source_repository="team/repo", source_commit="a" * 40
    )
    location = manifest["source_locations"]["revision/write.py"]
    selected = "demo/revisions/v2/revision/write.py" if revision == "v2" else "src/test_service/revision/write.py"
    raw = (snapshot / selected).read_bytes()
    assert raw == (destination / "test_service/revision/write.py").read_bytes()
    assert location["path"] == "packages/healthcare-sensor-app/" + selected
    assert location["sha256"] == hashlib.sha256(raw).hexdigest()
    assert location["verification"] == "declared" and location["commit"] == "a" * 40
    result = observe(destination, tmp_path)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual["verified"] is True and actual["source_locations"] == manifest["source_locations"]
    assert "text" not in location and "INSERT INTO" not in result.stdout


@pytest.mark.parametrize("change", ["missing", "extra_change"])
def test_fault_variant_missing_or_not_single_difference_fails_before_install(tmp_path, change):
    builder = builder_module()
    snapshot = tmp_path / "snapshot"
    builder.capture_source_snapshot(snapshot)
    variant = snapshot / "demo/revisions/v2/revision/write.py"
    if change == "missing":
        variant.unlink()
    else:
        variant.write_bytes(variant.read_bytes() + b"\n# unapproved additional change\n")
    destination = tmp_path / "installed"
    with pytest.raises((FileNotFoundError, ValueError)):
        builder.compile_revision("v2", destination, source_package=snapshot)
    assert not destination.exists()


@pytest.mark.parametrize(
    "field,value", [("sha256", "b" * 64), ("path", "elsewhere.py"), ("verification", "verified"), ("commit", "main")]
)
def test_runtime_rejects_detached_or_relabelled_location(tmp_path, field, value):
    builder = builder_module()
    destination = tmp_path / "installed"
    builder.compile_revision("v2", destination, source_repository="team/repo", source_commit="a" * 40)
    path = destination / "test_service/revision/_build_manifest.py"
    manifest = json.loads(path.read_text())
    manifest["source_locations"]["revision/write.py"][field] = value
    path.write_text(json.dumps(manifest))
    result = observe(destination, tmp_path)
    assert result.returncode != 0 and "Declared source location differs" in result.stderr


@pytest.mark.parametrize(
    "repository,commit",
    [("team/repo", ""), ("", "a" * 40), ("https://user:secret@host/repo", "a" * 40), ("team/repo", "main")],
)
def test_build_rejects_incomplete_or_unsafe_location(tmp_path, repository, commit):
    with pytest.raises(ValueError, match="Source location requires"):
        builder_module().compile_revision(
            "v2", tmp_path / "installed", source_repository=repository, source_commit=commit
        )


def test_undeclared_build_stays_without_git_claim_and_docker_copies_variant(tmp_path):
    manifest = builder_module().compile_revision("v2", tmp_path / "installed")
    assert "source_locations" not in manifest
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert "COPY demo/revisions/ demo/revisions/" in dockerfile
    assert '--source-repository "$SOURCE_REPOSITORY" --source-commit "$SOURCE_COMMIT"' in dockerfile


@pytest.mark.parametrize("revision", ["v1", "v2"])
def test_docker_style_in_place_cli_uses_captured_variant(tmp_path, revision):
    builder = builder_module()
    snapshot = tmp_path / "docker-context"
    builder.capture_source_snapshot(snapshot)
    original = (snapshot / "src/test_service/revision/write.py").read_bytes()
    fault = (snapshot / "demo/revisions/v2/revision/write.py").read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(snapshot / "demo/build_revision.py"),
            "--revision",
            revision,
            "--destination",
            str(snapshot / "src"),
            "--in-place",
            "--source-repository",
            "team/repo",
            "--source-commit",
            "a" * 40,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    manifest = json.loads(result.stdout)
    assert (snapshot / "src/test_service/revision/write.py").read_bytes() == (fault if revision == "v2" else original)
    assert observe(snapshot / "src", tmp_path).returncode == 0
    assert manifest["source_locations"]["revision/write.py"]["verification"] == "declared"
