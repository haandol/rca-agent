# B2 bounded-wait implementation — completed local handoff

## Result

The execution-only bounded wait is implemented, with the main review amendments and runner regression fixes. **338 focused integration tests passed**; Ruff lint/format and `git diff --check` passed. No implementation blocker remains. AWS behavior and model-driven discovery remain unverified; the parent owns build/deployment and the two live E2Es.

This is an implementation decision under the user's bounded-wait goal and autonomous-fix authorization. The detailed API and local implementation choices are not attributed to a verbatim detailed contract supplied by the user.

No AWS calls, product model calls, deployment, commits, baseline changes, artifact-validation edits/tests, analysis-pipeline edits, or main-owned demo/index edits were performed. Pre-existing `.playwright-cli/` is outside this change. The main's already committed cause-sidecar and failed-run documentation remain untouched.

## Evidence and corrected behavior

The local rehearsal `custlock-20260910T121625Z-1e41/retrospective.json` and its approved snapshot show successful step-2 StopTask ending at **2026-09-10 12:34:07.435002 UTC**. The exact owner's release event followed at **12:34:07.573070**, with `release_reason=sigterm` and `rollback_complete=true`. The alarm waiter returned at **12:36:33.148112**, before both required bins were complete. The model also cited `patient_vitals` read successes as writes. The execution became UNRESOLVED at **12:37:26.341857**.

The server now binds the first successful actual StopTask attempt from the approved prior action step in the current execution. It does not anchor to the later DescribeTasks/read or to a caller timestamp. Synthetic CLI output, input overrides, alternate credentials/endpoints, failed/read-only/unapproved/cross-step actions, and mismatched incident account/region are rejected as anchor evidence. The first bin always starts at `floor(ended_at_epoch/60)*60+60`, including an exact minute boundary. For the observed action, the immutable bins are **[12:35,12:36)** and **[12:36,12:37)** UTC.

The request, action identity, anchor, fixed window, alarm metadata and fixed deadline are appended and flushed before the first query. A workspace lock serializes wait calls. The same request returns its previous terminal receipt; different arguments or action for the same verification step are rejected. An interrupted wait cannot reset its budget or rebase its window.

Every generated AWS command passes the existing CLI gate and command-attempt audit. The tool can generate only CloudWatch GetMetricStatistics and DescribeAlarms reads. Polling is every 10 seconds using short cancellable Event.wait intervals; before the first bin can be complete, it checks control without issuing unusable metric queries. Each response's post-command UTC observation time determines completeness. Missing/partial values are unknown; pre-window/current bins do not count. Nonfinite, duplicate, misaligned and invalid values fail closed. An unhealthy complete fixed-bin value ends the wait immediately, including when another metric is missing or a CLI error includes a usable bad datapoint. Later model outcomes cannot overwrite a failed/incomplete wait to resolve the execution.

The wait is capped at 300 seconds and the remaining original execution budget; the existing MCP timeout remains 360 seconds. Commands are bounded by remaining time, with bounded child cleanup reserved inside that budget. Cancellation and the runner's claim heartbeat are checked throughout. The runner's local control file has an immutable deadline and fresh current-claim check; stale/inactive/error/expired control aborts observation. The added store helper performs only the existing consistent execution-item read and never renews claims.

## Approved criteria and discovery

Actual API:

```python
wait_for_post_action_metrics(
    step_id, action_step_id, metrics, failure_alarm_name, region,
    max_wait_seconds=300, latency_alarm_name="", completed_work_evidence=None,
)
```

`metrics` contains required `attempts` and `failures`, each with explicit observed `namespace`, `metric_name`, and a dimension name/value mapping. The operator first discovers these coordinates and the exact alarm through `run_playbook_command` using real `list-metrics`/`describe-alarms` results. Intact, unaltered observations from the same execution and region are required; no fixture names or invented defaults are encoded in the helper.

The current approved step-4 requires successful writes, zero failures in both bins, and its exact alarm OK. It **does not require latency**. A latency descriptor/alarm is optional and is accepted only when explicitly named by the approved criterion. Only that optional branch requires real query SampleCount > 0 and a nonbreaching latency value under the actual matching alarm's statistic, unit and threshold. Missing optional metadata never makes the current normal plan unusable.

Every complete bin requires attempts Sum > 0 and failures Sum = 0. The receipt always provides their arithmetic difference. It labels that difference `successful_writes` only with an actual source-bound completed-write accounting descriptor. The optional `completed_work_evidence` references an existing server-recorded producer log descriptor or approved context by `record_index` and `json_pointer`; it cannot supply counts or fabricated semantics. **The new producer descriptor is not required.** Without one, the receipt explicitly reports arithmetic, leaves `write_semantics_verified=false`, and tells the operator to collect real successful write-operation logs independently in the same fixed window. The current service's observed `ingest` write operation must not be confused with `patient_vitals` reads. Source/accounting semantics and operation identity must be discovered, never inferred from a counter name or fabricated.

Guidance now bounds historical log queries to the observed current incident and known owner stream, preferring `get-log-events` for a known stream. It preserves pagination, truncation and completeness checks. This addresses the observed unbounded FilterLogEvents scan that timed out at 300 seconds; it does not add a generic log observer. Exact owned release/rollback and every approved success criterion remain the operator's separate responsibility. The tool does not mark the whole execution RESOLVED.

The required execution MCP server and operator allowlist expose exactly the existing three execution tools plus `wait_for_post_action_metrics`. Tests inspect the actual in-process catalog and argument defaults. Analysis/execution tool separation remains intact.

## Evidence durability and runner regression fixes

The append-only `metric_wait_records` journal carries start, every poll, partial data/errors, and terminal receipts into assembled execution evidence and the existing S3 save path, including failed/cancelled/unresolved executions. Existing command attempts retain their timestamps, stdout/stderr, exit status, redaction, per-stream 20,000-character cap and explicit truncation counts. CLI outputs are preserved; HTTP metadata is preserved if present in those outputs, not invented. Retrospective output previews can shrink both command and duplicate wait-response streams under the existing JSON budget; durable evidence is unchanged. A deterministic pipeline test verifies publication before workspace cleanup.

Retrospective cancellation has its own callback watcher and stop event. It works after execution is RESOLVED without requiring an EXECUTING claim, and terminates only its own process. The pipeline supplies the shutdown callback to that watcher. Tests cover cancelled and noncancelled retrospective runs with inactive execution control.

If control publication and its secondary unlink both fail, cleanup logs the failure without masking the primary runner outcome. It never refreshes the old active heartbeat; the observer rejects that heartbeat when stale. Tests inject both errors and separately verify stale-control rejection.

## Verification

Final integration command from the repository root:

```sh
uv run --offline --directory packages/headless-codex pytest \
  tests/test_post_action_metrics.py tests/test_execution_mcp_server.py \
  tests/test_execution_evidence.py tests/test_execution_outcome.py \
  tests/test_execution_runner.py tests/test_execution_store.py \
  tests/test_execution_pipeline.py tests/test_execution_capabilities.py \
  tests/test_execution_harness_contracts.py tests/test_execution_context.py \
  tests/test_execution_request.py -q
```

Result: **338 passed**. The added deterministic tests exercise exact-boundary rounding, early OK/late data, fixed-window persistence/replay, missing/partial data, bad-bin stickiness with missing siblings and CLI errors, post-command completeness, invalid data, budget/cancel/claim and bounded process cleanup, anchor rejection, original incident scope, gate refusal, optional latency and metadata thresholds, completed-work semantics versus arithmetic, immutable request conflicts, interrupted waits, evidence publication, retrospective preview preservation, retrospective cancellation isolation, and double cleanup failure.

Ruff check and format-check passed for all **18 implementation-owned Python files**, including new files. The final workspace lint also checked two concurrently modified test files (20 files total); those two files were not edited by this implementation. `git diff --check` passed. Before the final integration pass, the affected runner/pipeline/wait tests passed (107 tests). Guidance delegation separately passed 29 focused tests; those are included in the final integration scope, not extra unique coverage.

Evidence logs:
- `/private/tmp/rca-customer-focus/bounded-wait-tests.log`
- `/private/tmp/rca-customer-focus/bounded-wait-lint.log`
- `/private/tmp/rca-customer-focus/bounded-wait-adr-check.log`
- `/private/tmp/rca-customer-focus/bounded-wait-changed-files.json`

## ADR coherence and limits

Existing Accepted `agent/0017` remains the owner. Its addendum now records next-minute rounding, the two immutable 60-second bins, maximum 300-second wait within the original budget/360-second tool timeout, cancellation/ownership checks, sticky failure, evidence retention, conditional write proof and optional approved latency. The matching mapping summary was updated in place; owner path/status/summary and the complete mapping/disk inventory were checked. No new ADR, provider, permission, or dependency was introduced. This was a targeted repository-local ADR sync; live environment access was not performed and deployed state is unverified.

Implementation limits are deliberate: current action anchoring supports ECS StopTask; metric health supports simple same-scope 60-second alarms (failure Sum, optional latency Average/Maximum/Sum with supported upper threshold operators). Metric math/composite alarms and unsupported metadata fail closed. Completed-work semantics are not inferred automatically from arbitrary application source code or names; real write logs remain valid independent proof when no observed producer descriptor exists. Reaching the existing retrospective metadata-only JSON ceiling still fails that existing projection explicitly while retaining S3 evidence. No local test establishes actual CloudWatch arrival latency, deployed IAM, or the model's live discovery/owned-release verification; those belong to the parent's E2E runs.

## Exact implementation-owned changed files (25)

- `docs/adr/.mapping.json`
- `docs/adr/agent/0017-playbook-execution-agent.md`
- `packages/headless-codex/AGENTS.md`
- `packages/headless-codex/harness/execution/AGENTS.md`
- `packages/headless-codex/harness/execution/agents/execution-operator.md`
- `packages/headless-codex/harness/execution/agents/execution-operator.toml`
- `packages/headless-codex/harness/execution/config.toml`
- `packages/headless-codex/src/headless_codex/adapters/secondary/codex/codex_execution_runner.py`
- `packages/headless-codex/src/headless_codex/adapters/secondary/execution/dynamodb_execution_store.py`
- `packages/headless-codex/src/headless_codex/execution_mcp_server.py`
- `packages/headless-codex/src/headless_codex/ports/interfaces/execution_runner.py`
- `packages/headless-codex/src/headless_codex/ports/interfaces/execution_store.py`
- `packages/headless-codex/src/headless_codex/services/execution_capabilities.py`
- `packages/headless-codex/src/headless_codex/services/execution_evidence.py`
- `packages/headless-codex/src/headless_codex/services/execution_outcome.py`
- `packages/headless-codex/src/headless_codex/services/execution_pipeline.py`
- `packages/headless-codex/src/headless_codex/services/execution_prompt.py`
- `packages/headless-codex/src/headless_codex/services/execution_workspace.py`
- `packages/headless-codex/tests/test_execution_capabilities.py`
- `packages/headless-codex/tests/test_execution_harness_contracts.py`
- `packages/headless-codex/tests/test_execution_pipeline.py`
- `packages/headless-codex/tests/test_execution_runner.py`
- `packages/headless-codex/tests/test_execution_store.py`
- `packages/headless-codex/src/headless_codex/services/post_action_metrics.py`
- `packages/headless-codex/tests/test_post_action_metrics.py`

Concurrent workspace edits observed and preserved, not authored by this implementation:
- `packages/headless-codex/tests/test_eval_alarm_metadata.py`
- `packages/headless-codex/tests/test_mcp_startup_harness_contract.py`
