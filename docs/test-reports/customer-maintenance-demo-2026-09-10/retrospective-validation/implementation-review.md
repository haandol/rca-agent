# Headless retrospective repair implementation

Completed narrow offline implementation against HEAD 4772c9645abe0fa08350bfd15240aca54256e647. Existing ADR agent/0018 already requires durable NO_CHANGE attestation; no ADR contract or main-owned documentation changed.

## Changes

- `packages/headless-codex/src/headless_codex/services/execution_evidence.py`: only when metadata exceeds the existing 60,000-character cap, share exactly equal command/intent/success_criteria/binding/request literals. Explicit typed paths identify null placeholders; literal contents never become references or instructions. Nonoverlapping paths and literal values avoid cycles/collisions. A focused resolver restores values in an independent copy. Existing preview omissions and unique-metadata failure remain.
- `packages/headless-codex/src/headless_codex/services/execution_prompt.py`: explain literal shared values, path resolution, and that absent previews cannot prove success or no defects.
- `packages/headless-codex/src/headless_codex/services/execution_pipeline.py`: persist full saved rationale, proposed update, effective empty update for NO_CHANGE and existing dashboard diff fields before publication. NO_CHANGE keeps its status, stores diff key and rationale summary; persistence failure blocks publication and records FAILED. UPDATED retains existing promotion rules. Exception diagnostics now expose bounded/redacted error type and detail without a traceback dump.
- `packages/headless-codex/tests/test_execution_pipeline.py`: public process_message tests use the real S3 evidence adapter with an in-memory client; verify readable NO_CHANGE/UPDATED attestation, full rationale over 4,000 characters, publication ordering, unchanged snapshot bytes, persistence failure/empty-key failure, and safe ValueError diagnostics.
- `packages/headless-codex/tests/test_retrospective_replay.py`: unchanged archived payload replay and full metadata reconstruction, preview omission counts, original immutability, synthetic negative/blocked/partial records, terminal bins, special keys/reference-shaped literals, exact equality, budget edges, unique overflow, empty/legacy evidence and invalid reference paths.
- `packages/headless-codex/tests/fixtures/retrospective-custlock-20260910.json`: self-contained unmodified evidence and pre-execution playbook with source IDs/SHA provenance. Structured/string credential redaction checks are semantically unchanged; AWS access-key, private-key and GitHub-token signature scans found no matches. No evidence redactions or ID substitutions were needed. Fixture is uncommitted for parent review.
- `/private/tmp/rca-customer-focus/retrofix-design.md`: design written before implementation.
- `/private/tmp/rca-customer-focus/retrofix-implementation.md`: this report.

## Actual archived replay

Source: `/private/tmp/rca-customer-focus/rehearsals/custlock-20260910T135052Z-48da/retrospective.json`

- Execution: `2dfebb2a-4551-45bc-b4a3-0cd9315f0968`; RCA: `82791526-e84b-5ae4-bfe4-c65ba1369480`; playbook: `9d2336c4-5ce4-4e96-84e7-b4de496ab81e`.
- Original evidence JSON: 245,919 characters, 4 steps, 34 attempts, 20 wait records (18 polls, started and terminal).
- New prompt evidence JSON: **59,892 characters**, valid JSON, within unchanged **60,000** cap. 9 literal groups / 48 explicit reference paths; existing output-preview omission accounting records 159,425 omitted characters.
- Reconstructed every metadata field equivalently; only the already allowed output previews differ, each with verified prefix/omission markers. Input object and archived source bytes unchanged. Fixture evidence equals the actual archived payload.
- Source-file SHA-256 before/after: `2d058e286d578803a73c7f345f3bfafe4ceb5d9636a9d60e088e359519dc7762`.
- Canonical evidence SHA-256 before/after: `904fc69c3ff76b6ace2f19121af753b5077d980aeb4f87a061947fee51efef52` (`json.dumps(..., ensure_ascii=False, sort_keys=True).encode()`, matching the archived offline replay).
- The archived run remains retrospective **FAILED**; physical recovery is not relabeled as a full E2E pass. Synthetic failure/blocked/partial cases are separate from the unchanged actual fixture.

## Verification

Working directory: `packages/headless-codex`.

```text
uv run --no-sync pytest tests/test_retrospective_replay.py tests/test_execution_evidence.py tests/test_execution_pipeline.py tests/test_retrospective_prompt_contracts.py tests/test_evidence_store.py tests/test_post_action_metrics.py -q
160 passed in 2.56s (17 new regression cases; prior focused run also 160 passed in 3.12s)

uv run --no-sync ruff check src/ tests/
All checks passed!

uv run --no-sync ruff format --check src/headless_codex/services/execution_evidence.py src/headless_codex/services/execution_pipeline.py src/headless_codex/services/execution_prompt.py tests/test_execution_pipeline.py tests/test_retrospective_replay.py
5 files already formatted

git diff --check -- packages/headless-codex  # from repository root
Passed
```

The first Ruff run found only test-double naming/style issues, repaired before the final passing checks. Final direct archived replay independently rechecked prompt extraction, full reconstruction and both unchanged SHA values.

No AWS/model calls, deployment, commit, goal API, generated run/result edits, Healthcare/other-package or main-owned document edits. API, approved snapshot, CLI gate, first fixed windows and health checks remain unchanged. No child delegates were created; none remain open. Parent owns full suite, commit, four actual CLI catalogs, deployment and two fresh full passes. Model understanding of the reference representation requires that later live validation; this report claims offline verification only.

Report complete; file mutation stopped.
