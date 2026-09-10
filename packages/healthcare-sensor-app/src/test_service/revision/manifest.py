"""Verify the source installed by the image builder and expose its fingerprint."""

import hashlib
import json
from pathlib import Path


def source_manifest() -> dict:
    """Hash the installed Python source; reject changed files in built images."""
    root = Path(__file__).resolve().parents[1]
    files = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
        if path.name != "_build_manifest.py"
    }
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest_path = root / "revision" / "_build_manifest.py"
    if manifest_path.exists():
        # This file is JSON despite its suffix so setuptools always includes it.
        expected = json.loads(manifest_path.read_text())
        if expected["files"] != files:
            raise RuntimeError("Installed source differs from the build manifest")
        return {**expected, "verified": True}
    return {"revision": "r1", "fingerprint": fingerprint, "files": files, "verified": False}
