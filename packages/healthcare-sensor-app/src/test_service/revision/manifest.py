"""Verify the source installed by the image builder and expose its fingerprint."""

import hashlib
import json
import re
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
        if expected["files"] != files or expected.get("fingerprint") != fingerprint:
            raise RuntimeError("Installed source differs from the build manifest")
        locations = expected.get("source_locations")
        if locations is not None:
            location = locations.get("revision/write.py") if isinstance(locations, dict) else None
            path = (
                "packages/healthcare-sensor-app/demo/revisions/v2/revision/write.py"
                if expected.get("revision") == "v2"
                else "packages/healthcare-sensor-app/src/test_service/revision/write.py"
            )
            if (
                not isinstance(location, dict)
                or set(locations) != {"revision/write.py"}
                or set(location) != {"repository", "commit", "path", "sha256", "verification"}
                or location.get("path") != path
                or location.get("sha256") != files["revision/write.py"]
                or location.get("verification") != "declared"
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
                    str(location.get("repository", "")),
                )
                or not re.fullmatch(r"[a-f0-9]{40}", str(location.get("commit", "")))
            ):
                raise RuntimeError("Declared source location differs from the installed file binding")
        # verified applies to installed bytes only; source_locations still require an actual Git read.
        return {**expected, "verified": True}
    return {"revision": "v1", "fingerprint": fingerprint, "files": files, "verified": False}
