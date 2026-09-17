"""Validate optional repository locators without treating declarations as Git evidence."""

import hashlib
import json
import re
from pathlib import PurePosixPath


def declared_source_locations(message: dict) -> dict:
    """Retain only immutable relative locators bound to a verified installed manifest hash."""
    files = message.get("files")
    locations = message.get("source_locations")
    if (
        message.get("event") != "source_manifest"
        or message.get("verified") is not True
        or not isinstance(files, dict)
        or not isinstance(locations, dict)
        or hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != message.get("fingerprint")
    ):
        return {}
    result = {}
    for installed, value in locations.items():
        if not isinstance(installed, str) or not isinstance(value, dict):
            continue
        repository, commit, path, digest = (value.get(key) for key in ("repository", "commit", "path", "sha256"))
        if (
            set(value) != {"repository", "commit", "path", "sha256", "verification"}
            or value.get("verification") != "declared"
            or not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository)
            or not isinstance(commit, str)
            or not re.fullmatch(r"[a-f0-9]{40}", commit)
            or not isinstance(path, str)
            or not path.endswith("/" + installed)
            or any(
                PurePosixPath(item).is_absolute() or ".." in PurePosixPath(item).parts or "\\" in item
                for item in (installed, path)
            )
            or digest != files.get(installed)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            continue
        result[installed] = dict(value)
    return result
