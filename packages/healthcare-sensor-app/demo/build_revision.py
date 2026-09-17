"""Compile a revision by copying source, selecting one INSERT column, and hashing it.

This is a build tool, not a runtime selector. Destination must be a fresh tree.
Docker uses --in-place after COPY; local proofs compile separate temporary trees.
"""

import argparse
import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
FAULT_SOURCE = Path("demo/revisions/v2/revision/write.py")
REPO_PACKAGE = "packages/healthcare-sensor-app"


def source_files(root: Path) -> dict[str, str]:
    """Fingerprint installed Python files without build artifacts."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
        if path.name != "_build_manifest.py"
    }


def fingerprint(files: dict[str, str]) -> str:
    """Hash a sorted file map so equivalent source trees have the same identity."""
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def capture_source_snapshot(destination: Path) -> dict:
    """Freeze one verified base and worker before compiling revisions.

    Read twice to reject edits during capture. Every revision and phase then
    reads this private snapshot, never the concurrently edited workspace.
    """

    def inputs():
        """Select only source and proof code, keeping credentials and generated outputs out."""
        return sorted(
            [
                *PACKAGE.joinpath("src/test_service").rglob("*.py"),
                PACKAGE / "demo/local_worker.py",
                PACKAGE / "demo/build_revision.py",
                PACKAGE / FAULT_SOURCE,
            ]
        )

    captured_at = datetime.now(UTC).isoformat()
    payloads = {
        str(path.relative_to(PACKAGE)): path.read_bytes() for path in inputs() if path.name != "_build_manifest.py"
    }
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in payloads.items()}
    observed = {
        str(path.relative_to(PACKAGE)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in inputs()
        if path.name != "_build_manifest.py"
    }
    if observed != hashes:
        raise RuntimeError("Workspace sources changed during snapshot capture")
    destination.mkdir(parents=True, exist_ok=False)
    for relative, data in payloads.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {"captured_at": captured_at, "fingerprint": fingerprint(hashes), "files": hashes}


def compile_revision(
    revision: str,
    destination: Path,
    *,
    in_place: bool = False,
    source_package: Path | None = None,
    source_repository: str = "",
    source_commit: str = "",
) -> dict:
    """Build exactly one known revision and record all installed source hashes."""
    if revision not in ("v1", "v2"):
        raise ValueError("Unknown source revision")
    if in_place and (destination / "test_service" / "revision" / "_build_manifest.py").exists():
        raise ValueError("Refusing to relabel an already compiled source tree")
    package = source_package or PACKAGE
    if bool(source_repository) != bool(source_commit) or (
        source_repository
        and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", source_repository)
            or not re.fullmatch(r"[a-f0-9]{40}", source_commit)
        )
    ):
        raise ValueError("Source location requires repository owner/name and full lowercase commit SHA")
    normal = (package / "src/test_service/revision/write.py").read_bytes()
    fault = (package / FAULT_SOURCE).read_bytes()
    if normal.count(b'TIMESTAMP_COLUMN = "timestamp"') != 1 or fault != normal.replace(
        b'TIMESTAMP_COLUMN = "timestamp"', b'TIMESTAMP_COLUMN = "sampled_at"'
    ):
        raise ValueError("Checked-in fault source must differ only in the timestamp column constant")
    base_fingerprint = fingerprint(source_files(package / "src/test_service"))
    if not in_place:
        shutil.copytree(
            package / "src", destination, ignore=shutil.ignore_patterns("__pycache__", "_build_manifest.py")
        )
    root = destination / "test_service"
    # Always start from stable source, including repeated local builds.
    if not in_place:
        assert (root / "revision" / "session.py").exists()
    write_path = root / "revision" / "write.py"
    if write_path.read_bytes() != normal:
        raise ValueError("Destination write source differs from the captured normal source")
    if revision == "v2":
        write_path.write_bytes(fault)
    files = source_files(root)
    manifest = {
        "revision": revision,
        "fingerprint": fingerprint(files),
        "base_fingerprint": base_fingerprint,
        "files": files,
    }
    if source_repository:
        relative = FAULT_SOURCE if revision == "v2" else Path("src/test_service/revision/write.py")
        manifest["source_locations"] = {
            "revision/write.py": {
                "repository": source_repository,
                "commit": source_commit,
                "path": f"{REPO_PACKAGE}/{relative.as_posix()}",
                "sha256": files["revision/write.py"],
                # A build argument is a locator, never proof of Git ownership or contents.
                "verification": "declared",
            }
        }
    (root / "revision" / "_build_manifest.py").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", choices=("v1", "v2"), default="v1")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--source-repository", default="")
    parser.add_argument("--source-commit", default="")
    args = parser.parse_args()
    print(
        json.dumps(
            compile_revision(
                args.revision,
                args.destination,
                in_place=args.in_place,
                source_repository=args.source_repository,
                source_commit=args.source_commit,
            )
        )
    )
