# Maintenance incident: measured AWS input, failed RCA rehearsal

This directory preserves evidence from `custlock-20260910T063725Z-0ea4`.
The rehearsal's analysis failed because its MCP evidence tools were unavailable;
it produced no successful RCA/playbook or approved remediation. These files are
source observations for a future **provided-observation model evaluation**, not
model results, baseline approval, or a successful deployed E2E test.

Only `tests/scenarios/maintenance-transaction-lock.json` changes its source.
Its `unsupported` taxonomy, six required evidence IDs, four root evidence IDs,
two independent competing-cause judgments, confirmed-root requirement, artifacts,
and executable safe DRAFT requirement are unchanged. The other three active
scenarios and the evaluator/baseline are unchanged.

## Original bytes and provenance

`capture-manifest.json` records every archived file's SHA-256 and original path.
The `.json.txt` and `.py.txt` files are byte-for-byte copies, protected from JSON
formatting. No AWS calls were made while assembling this dataset.

| Archived source key | Original evidence | Use |
|---|---|---|
| `baseline` | Journal `000000.json`, 06:37:48.049514 UTC | Original service task/definition, image, installed source logs, pool settings, successful preincident metrics |
| `definition` | Journal `000005.json`, 06:38:11.920955 UTC | Actual registered maintenance task definition and its Python entrypoint/command |
| `statusBefore` | Journal `000016.json`, 06:39:34.710566 UTC; observation behind `004-status.stdout.json` | RUNNING maintenance task, owner tags, startedBy, image/definition |
| `statusIncident` | Journal `000018.json`, 06:40:14.542287 UTC; observation behind `005-status.stdout.json` | Same owned task and service task, incident metrics |
| `alarmStatus` | Journal `000024.json`, fetched at 06:42:11.358922 UTC | **Only** the historical ALARM transition at 06:41:40.992 UTC, its unchanged configuration and evaluated periods |
| `maintenanceLogs` | `maintenance-logs.json` | Installed source manifest, task runtime identity, lock-acquisition event |
| `blockers` | `blocker-sample.json`, response dated 06:41:44 UTC | DB observations whose event timestamps are 06:39:43.209 and 06:39:44.217 UTC |
| `analysisStart` | `new-sessions.json` | Operator-only cutoff evidence: session creation at 06:41:41.260905 UTC |
| `maintenanceSource` | Repository `test_service/maintenance.py` | Exact bytes matching the startup manifest; executable signal/transaction cleanup code |
| `sessionSource` | Repository `test_service/revision/session.py` | Exact bytes matching the service's startup manifest; session lifetime code |
| `localPredecessor` | Previous maintenance scenario JSON | Unmodified local input, expectation and historical projection; never joined to AWS records |

The controller journal is an observation archive, not AWS-signed attestation.
Journal `at` is its recorded publication time, not an invented per-API timestamp.
Repository source is admitted only when its complete-file hash matches the
observed installed manifest. Source behavior is not evidence that a signal,
rollback or recovery actually happened.

## Incident identity and cutoff

The acquisition event names backend **5489** and the recorded run ID.
The DB snapshot names the same backend and
`healthcare-maint-custlock-20260910T063725Z-0ea4`; backend **5212** is waiting on it.
The startup identity records the full task ARN ending
`ca14a8288c8b46b8a7460c3800a7932b`. Incident task descriptions match that ARN,
cluster, immutable definition, image digest, run/journal tags and startedBy.
The separate task's definition invokes `test_service.maintenance` for this run
and schema. No task ARN is inferred from a DB PID or borrowed from the local run.

The cutoff is **2026-09-10T06:41:41.260905+00:00**, the recorded start of analysis.
The projection distinguishes event time from later collection time:

- Current-state task/service descriptions must have been journaled before cutoff.
- Historical CloudWatch events and alarm transitions retain their original
  timestamps, even if collected afterward.
- Metric periods must be complete before cutoff. The alarm's evaluated 06:39 and
  06:40 periods qualify; the later response's 06:41 metric period does not.
- The acquisition's scheduled expiry is a future deadline known at acquisition,
  **not** an observed expiry or recovery.
- No stop response, release/rollback event, recovery metric, approval, execution
  outcome, retrospective result, or operator success/cleanup flag is model input.
  Even the later alarm response's task state is excluded.

## Reproducible observation mapping

`projectIncidentCaptures` dispatches this source kind to
`projectAwsMaintenanceCaptures` in `tests/harness/scenario-capture-boundary.mjs`.
It checks source hashes, timestamps and identity bindings before projecting.
`operator-capture-map.json` links neutral capture IDs to archived source keys and
JSON pointers. Source excerpts include their real file hash and line numbers,
excluding comments/docstrings. The manifest, operator mapping and outcome labels
are not passed to the models.

| Model observation | Measured input |
|---|---|
| `obs-01` | Baseline successful ingest and incident failures/query-duration metrics; actual alarm transition/configuration |
| `obs-02` | Lock acquisition, DB waiter/blocker, runtime identity and incident-time owned RUNNING task |
| `obs-03` | Original service image/source and maintenance definition/image/source; source-backed SIGTERM handling and rollback |
| `obs-04` | Actual DB wait then idle-backend snapshots with original query/transaction timestamps |
| `obs-05` | Same backend becomes idle with no open transaction, pool checked-out falls from 1 to 0 while the maintenance lock remains; installed session-cleanup code |
| `obs-06` | Recorded unchanged service definition/configuration; pool size 5, checked-out 1 then 0, database connections 4 versus limit 79 |

This AWS capture does **not** contain the old local run's request-specific SQL
durations, statement outcomes, or checkout/checkin event counts. Their absence is
explicit in the projected observations. The snapshots and source code must be
interpreted as the evidence actually available, not reconstructed counters or a
claim that every possible connection leak was disproved.

The owned-task observations are historical context for a DRAFT playbook, not
permission to stop a currently running task. The existing approval and live
pre-action ownership checks remain necessary. No successful remediation was
simulated to make this fixture positive.

## Offline verification

Run `node --test tests/harness/realistic-scenarios.test.mjs`.
The contracts reproduce the exact alarm/observations and source mapping, preserve
the local predecessor, reject hash/binding/cutoff violations, exclude later metric
periods, and prove changes to recovery/cleanup/operator fields do not affect model
input. Existing mandatory negative probes remain unchanged. Structural test
objects are never saved as model results; prior failed model outputs remain intact.
