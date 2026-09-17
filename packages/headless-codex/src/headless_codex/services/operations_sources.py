"""Read associated CI configuration separately from immutable incident/deployed source evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from headless_codex.services.analysis_part_workspace import write_once
from headless_codex.services.analysis_parts import json_bytes

_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


def associated_repositories(context: dict, configured: str = "") -> set[str]:
    """Only configured or actually read source repositories can supply current CI evidence."""
    values = {configured}
    sources = list(context.get("incident", {}).get("source_artifacts", []))
    sources += context.get("root_read_receipts", {}).get("sources", [])
    values.update(source.get("repository", "") for source in sources if isinstance(source, dict))
    return {value for value in values if isinstance(value, str) and _REPOSITORY.fullmatch(value)}


class _NoRedirect(HTTPRedirectHandler):
    """Do not forward the configured credential outside the fixed GitHub API destination."""

    def redirect_request(self, request, fp, code, message, headers, newurl):
        """Treat redirects as unavailable instead of allowing a model read to change repository authority."""
        raise HTTPError(request.full_url, code, "redirect not permitted", headers, fp)


def read_ci_source(
    repository: str,
    path: str,
    revision: str,
    *,
    context: dict,
    token: str,
    configured_repository: str,
    timeout: float,
    fetch=None,
) -> dict:
    """Fetch one associated configuration via GET, verify exact blob bytes and label it as a current CI read."""
    if repository not in associated_repositories(context, configured_repository):
        raise ValueError("repository is not associated with this incident or configured deployment")
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("invalid repository-relative CI path")
    if not (
        path.startswith((".github/workflows/", ".circleci/", "ci/", ".buildkite/"))
        or path
        in {
            ".gitlab-ci.yml",
            "Jenkinsfile",
            "azure-pipelines.yml",
            "buildspec.yml",
            "Dockerfile",
            "pyproject.toml",
            "package.json",
        }
    ):
        raise ValueError("path is not a supported explicit CI/control configuration")
    if not revision or any(ord(c) < 32 for c in revision) or timeout <= 0:
        raise ValueError("CI source revision or remaining read budget unavailable")
    url = f"https://api.github.com/repos/{repository}/contents/{quote(path, safe='/')}?{urlencode({'ref': revision})}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "rca-readonly-ci"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    if fetch is None:
        fetch = build_opener(_NoRedirect()).open
    with fetch(request, timeout=min(timeout, 60)) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("CI source exceeds bounded read size; no partial source accepted")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get("type") != "file"
        or value.get("path") != path
        or value.get("encoding") != "base64"
    ):
        raise ValueError("CI response is not the exact requested text file")
    content = base64.b64decode("".join(value["content"].split()), validate=True)
    text = content.decode("utf-8")
    if hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() != value.get("sha"):
        raise ValueError("CI blob bytes do not match the returned source hash")
    return {
        "kind": "current_ci",
        "evidence_scope": "repository_file_snapshot",
        "effectiveness_status": "UNVERIFIED",
        "repository": repository,
        "path": path,
        "requested_revision": revision,
        "revision_kind": "git_blob_snapshot",
        "blob_sha": value["sha"],
        "text": text,
        "sha256": hashlib.sha256(content).hexdigest(),
        "observed_at": datetime.now(UTC).isoformat(),
        "source_ref": f"github://{repository}@{value['sha']}/{path}",
    }


def save_ci_source(execution_token: str, source: dict) -> str:
    """Append one content-addressed read receipt; it never replaces the frozen incident or another read."""
    digest = hashlib.sha256(json_bytes(source)).hexdigest()
    filename = f"operations-source-{digest}.json"
    write_once(execution_token, filename, source)
    return filename


def qualify_operations(result: dict, sources: list[dict]) -> dict:
    """Only actual control/configuration evidence can support OBSERVED; missing CI remains unverified."""
    from copy import deepcopy

    result = deepcopy(result)
    refs = {
        source["source_ref"]
        for source in sources
        if source.get("kind") == "current_ci"
        and isinstance(source.get("text"), str)
        and source.get("sha256") == hashlib.sha256(source["text"].encode()).hexdigest()
    }
    unavailable = False
    for finding in result["findings"]:
        if finding["status"] == "OBSERVED" and not (set(finding["evidence_refs"]) & refs):
            finding["status"] = "UNVERIFIED"
            unavailable = True
    if unavailable:
        result["limitations"].append("실제로 읽은 CI/운영 설정 원문에 연결되지 않은 판단은 미확인입니다.")
    return result
