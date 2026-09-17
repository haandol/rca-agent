"""Separate model artifact tools for recovery, code preview and operations; no control or GitHub writes."""

from __future__ import annotations

import json
import os
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field, StrictInt

from headless_codex.services.analysis_part_contract import (
    validate_code_proposal,
    validate_operations,
    validate_recovery_narrative,
)
from headless_codex.services.analysis_part_workspace import (
    CONTEXT_NAME,
    CONTROL_NAME,
    RESULT_FILES,
    fixed_path,
    read_object,
    require_role,
    write_once,
)
from headless_codex.services.execution_context import RUN_TOKEN_ENV
from headless_codex.services.execution_evidence import redact
from headless_codex.services.retrospective_reader import _model_view, _page, _pointer

recovery = FastMCP("recovery-result")
code_preview = FastMCP("code-preview")
operations = FastMCP("operations-result")


def _context(token: str) -> dict:
    """Expose prior immutable parts to later roles without altering the frozen incident object."""
    value = read_object(token, CONTEXT_NAME)
    for part in ("recovery", "root_cause", "operations"):
        name = f"analysis-part-{part}.json"
        if fixed_path(token, name).is_file():
            value[part] = read_object(token, name)
    source_file = "root-source-artifacts.json"
    if fixed_path(token, source_file).is_file():
        value["root_read_receipts"] = read_object(token, source_file)
    value["saved_role_results"] = {
        role: read_object(token, name) for role, name in RESULT_FILES.items() if fixed_path(token, name).is_file()
    }
    value["operations_sources"] = [
        read_object(token, path.name)
        for path in sorted(fixed_path(token, CONTEXT_NAME).parent.glob("operations-source-*.json"))
        if not path.is_symlink()
    ]
    from headless_codex.services.operations_sources import associated_repositories

    value["associated_repositories"] = sorted(associated_repositories(value, os.environ.get("GITHUB_REPOSITORY", "")))
    return value


def _check_owner() -> None:
    """Production artifacts require the current live claim; standalone evaluation has no cloud session."""
    from headless_codex.mcp_server import _runtime_session

    runtime = _runtime_session()
    if runtime is not None:
        store, _, rca_id, claim = runtime
        if store.is_terminated(rca_id, claim_token=claim):
            raise ValueError("analysis ownership is no longer held")


@recovery.tool()
@code_preview.tool()
@operations.tool()
def read_analysis_context(
    pointer: str = "",
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    max_items: Annotated[StrictInt, Field(ge=1, le=50)] = 20,
    text_chars: Annotated[StrictInt, Field(ge=1, le=4000)] = 4000,
) -> str:
    """Read only this run's frozen incident and prior results, as a redacted paginated model view.

    Start at the empty pointer for an index. Nested values require their returned pointer;
    next_offset means another page. complete ends the selected stored value, not omitted
    upstream output. Never interpret truncated output or missing source as absence of a control.
    """
    try:
        _check_owner()
        token = os.environ.get(RUN_TOKEN_ENV, "")
        source = _context(token)
        view = _model_view(json.dumps(source, ensure_ascii=False).encode())
        reply = _page(
            _pointer(view, pointer),
            pointer,
            offset,
            max_items,
            text_chars,
            {"view": "redacted_frozen_analysis_context"},
        )
    except Exception:
        reply = {"ok": False, "error": "context unavailable or invalid selector; do not infer missing evidence"}
    return json.dumps(reply, ensure_ascii=False)


def _save(role: str, result_json: str) -> str:
    """Validate only the caller role's result and retain immutable local output for fenced publication."""
    try:
        token = os.environ.get(RUN_TOKEN_ENV, "")
        require_role(token, role)
        context = _context(token)
        _check_owner()
        value = json.loads(result_json)
        if role == "recovery":
            value = validate_recovery_narrative(value)
        elif role == "operations":
            from headless_codex.services.operations_sources import qualify_operations

            value = qualify_operations(validate_operations(value), context["operations_sources"])
        else:
            value = validate_code_proposal(
                value,
                [
                    *context["incident"].get("source_artifacts", []),
                    *context.get("root_read_receipts", {}).get("sources", []),
                ],
            )
        write_once(token, RESULT_FILES[role], value)
        _check_owner()
        require_role(token, role)
        return json.dumps({"ok": True})
    except Exception as exc:
        return json.dumps({"ok": False, "error": redact(str(exc))[:500]}, ensure_ascii=False)


@recovery.tool()
def save_recovery_result(result_json: str) -> str:
    """Save recovery title, summary, reason, evidence_refs, limitations and recommendation.

    recommendation is ROLLBACK or UNAVAILABLE; declining remains UNAVAILABLE even when native checks pass.
    Do not include playbook, verification or eligibility: those come from server observations.
    This tool never executes recovery and cannot make model-supplied baseline claims authoritative.
    """
    return _save("recovery", result_json)


@code_preview.tool()
def save_code_proposal(result_json: str) -> str:
    """Save a preview matching actually read source and exact line/diff bytes, or UNAVAILABLE.

    Fields: status, title, optional repository/base_revision, files, test_plan,
    tests_status=NOT_RUN, limitations. Files carry path/start_line/end_line/original/
    proposed/unified_diff/evidence_refs. This never creates a branch or publishes a PR.
    """
    return _save("code_proposal", result_json)


@operations.tool()
def save_operations_result(result_json: str) -> str:
    """Save evidence-linked findings and proposed operational checks; no CI or service mutation.

    Fields: title, summary, findings, recommendations, limitations. Finding status is
    OBSERVED (requires evidence_refs) or UNVERIFIED. Recommendations require title,
    description, stage, priority, check, failure_condition, verification_plan,
    validation_status=NOT_RUN, evidence_refs and optional owner.
    """
    return _save("operations", result_json)


@operations.tool()
def read_ci_configuration(repository: str, path: str, revision: str) -> str:
    """Read an associated repository's explicit CI/control file through a server-owned GET only.

    Read associated_repositories from read_analysis_context first. Supply an exact
    repo-relative configuration path and explicit branch/commit. The response identifies
    a current CI snapshot, never the deployed incident source or a completed test run.
    Returned source_ref can support findings; its body is paged via operations_sources.
    """
    import time

    from headless_codex.services.operations_sources import read_ci_source, save_ci_source

    try:
        token = os.environ.get(RUN_TOKEN_ENV, "")
        context = require_role(token, "operations")
        if context.get("model_eval"):
            raise ValueError("model-eval cannot perform live CI reads")
        _check_owner()
        context = _context(token)
        control = read_object(token, CONTROL_NAME)
        source = read_ci_source(
            repository,
            path,
            revision,
            context=context,
            token=os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", ""),
            configured_repository=os.environ.get("GITHUB_REPOSITORY", ""),
            timeout=control["deadline_epoch"] - time.time(),
        )
        _check_owner()
        require_role(token, "operations")
        save_ci_source(token, source)
        return redact(
            json.dumps(
                {
                    "ok": True,
                    "source_ref": source["source_ref"],
                    "sha256": source["sha256"],
                    "observed_at": source["observed_at"],
                    "read_body_at": "/operations_sources",
                }
            )
        )
    except Exception:
        return json.dumps(
            {
                "ok": False,
                "error": "CI source unavailable; mark related findings UNVERIFIED and proposed checks NOT_RUN",
            }
        )
