# Three-part analysis wire contract

This is the shared implementation contract for Strands, Headless and Dashboard.
The analysis workflow is `recovery-first-v1`. Existing records without this workflow
and without part records retain the legacy completed-report path.

## Identifiers and storage

The three ordered part names are `recovery`, `root_cause`, `operations`.
Engine is `strands` or `headless-codex`. Public readers bind every value to the
requested RCA and engine; neither model output nor a client-provided S3 key chooses
the source.

DynamoDB uses the existing table:

- Parent: `PK=RCA#<rca_id>, SK=ANALYSIS#SESSION`.
- Frozen incident: `PK=RCA#<rca_id>, SK=INCIDENT_SNAPSHOT`.
- Current part: `PK=RCA#<rca_id>, SK=<engine>#ANALYSIS_PART#<part>`.
- Immutable part revision metadata:
  `PK=RCA#<rca_id>, SK=<engine>#ANALYSIS_PART_VERSION#<part>#<revision>`.

The configured evidence bucket holds exact UTF-8 JSON bytes at:

- `analysis-parts/<engine>/<rca_id>/incident/<sha256>.json`
- `analysis-parts/<engine>/<rca_id>/<part>/<sha256>.json`

Hashes are lowercase SHA-256 hex without a prefix. `revision` is the full payload
hash. The `runbook_digest` is a **different hash**, calculated with the existing
canonical approval serializer over the complete `playbook` object only.

Write S3 create-only, verify existing bytes on an idempotent retry, then commit the
DynamoDB reference under the parent's current claim. Failed object persistence
must not publish a part. Stored keys, payload identity and hashes are validated on
read. S3 is not chosen from model/client input.

## Lightweight records

Each part record contains:

```text
schema_version: 1
workflow: recovery-first-v1
rca_id, engine, part
status: WAITING | RUNNING | COMPLETED | FAILED | SKIPPED
revision: payload SHA-256 (present once a result/failure payload is published)
payload_s3_key, payload_sha256
incident_s3_key, incident_sha256
claim_token, attempt
created_at, updated_at, started_at?, completed_at?
summary: bounded display text
error: bounded reason for FAILED/SKIPPED
body_expires_at: epoch seconds
ttl: epoch seconds
approval_status: READY | UNAVAILABLE | REVOKED (recovery only)
runbook_digest: canonical playbook digest (READY recovery only)
```

The full incident and all model outputs belong in S3, never in this metadata.
Original payload and light state use the existing 60/90-day retention periods;
duplicate publication does not extend retention.

Only actual start enters RUNNING. Completion requires persisted payload and the
claim-fenced metadata commit. FAILED and SKIPPED carry reasons. A completed
revision is immutable. Exact duplicate publication returns the same result;
different content must not silently replace it. An explicit new revision requires
the expected current revision in the conditional commit and preserves the old
revision. A recovery result is not automatically regenerated on redelivery:
reuse its frozen incident and existing result. Later parts cannot write recovery.

The workflow marker and first stage registration are claim-fenced. This does not
change the parent to COMPLETED. Existing inner RCA state transitions and budgets
remain in force. Terminal publication permits the active writer's claim only;
late writes after failure/cancel/claim replacement are rejected.

`complete_analysis` accepts optional `side_effect_lease_token`. A supplied token must
match the current unexpired lease and parent claim; completion atomically consumes
the lease. The caller must not release that consumed lease afterward. Without a
token, completion refuses an active or malformed lease. `analysis_parts_finalized`
marks the durable all-parts outcome, including root FAILED, so redelivery retries
only pending notification/publication handoff rather than rerunning immutable parts.


## Frozen incident

The server freezes this before publishing recovery:

```json
{
  "schema_version": 1,
  "rca_id": "<server RCA>",
  "engine": "<server engine>",
  "alarm": {},
  "scoping": {},
  "observations": {},
  "source_artifacts": []
}
```

The incident record is first-write-wins across engines. Its `engine` is the originating
writer, not necessarily the current claim owner. The payload and S3 key retain that
origin engine. Reads validate the canonical record, same RCA, allowed origin engine,
origin-engine path and hash. Engine takeover reuses this snapshot even after rollback;
current recovery-control eligibility may be refreshed separately and must not replace
historical incident evidence. Part records remain engine-scoped.

`alarm` is the pinned original alarm; `scoping` is the current-run scoping result;
`observations` are server-owned baseline/current observations, including source
references and the failure cutoff. Exact incident evidence must survive rollback.
`source_artifacts` contains only actually read code with its source identity,
path, exact text and content hash. It can be empty; that means exact code-change
previews may remain unavailable. Never populate it with model-invented source.

Model-eval uses supplied historical observations and local artifacts. It must not
enable live observation or early approval simply because a model supplied a
baseline flag or control ARN.

## Part payload envelope

```json
{
  "schema_version": 1,
  "workflow": "recovery-first-v1",
  "rca_id": "<server RCA>",
  "engine": "<server engine>",
  "part": "recovery",
  "status": "COMPLETED",
  "incident_ref": {"key": "<fixed key>", "sha256": "<fixed hash>"},
  "input_refs": [],
  "result": {},
  "limitations": []
}
```

The server owns identity, status, references and eligibility. Each role supplies
only its own result. Failed/skipped envelopes preserve a reason and available
evidence without pretending to contain a successful role output.

`recovery.result`:

```text
title, summary, reason, evidence_refs[], limitations[]
recommendation: ROLLBACK | UNAVAILABLE
playbook: complete current-run Playbook object, or null
verification: server-produced validation outcome and input compatibility evidence
```

READY requires an exact approved-contract rollback plan and server-verified
baseline/current scope, target, settings, original successful writes and input
compatibility. Reuse existing runbook/observed-plan validation; do not globally
remove the old root-confirmed guard. The new recovery path alone is independent
of root confirmation. Missing compatibility evidence returns UNAVAILABLE, not a
guessed READY. Store the complete playbook snapshot without later root/ops changes.

`root_cause.result`:

```text
title, summary
root_cause: description, confirmed, confidence, selected_hypothesis_id
report_markdown: existing supported analysis detail, including evidence/5 Whys/timeline
code_proposal:
  status: PROPOSED | UNAVAILABLE
  title, repository?, base_revision?
  files[]: path, start_line, end_line, original, proposed, unified_diff, evidence_refs[]
  test_plan[], tests_status: NOT_RUN | PASSED | FAILED
  limitations[]
```

For lossless finalization and redelivery, the server also preserves `report` (the full
structured report), `selected_hypothesis`, `validated_fault_type`, and separately
qualified `source_artifacts` / `control_artifacts` in this result. These are source
snapshots, not model-owned authority fields. Final report storage and public
knowledge publication reuse these records; early approval still reads recovery only.

Root metadata comes from the existing server-owned selection/validation.
A PROPOSED code change must match actually read source bytes and location;
otherwise expose UNAVAILABLE/limitations without a fabricated diff. A source
snapshot is identified as a snapshot, not falsely as a Git commit.
No branch/PR publication or GitHub write tool is added.

`operations.result`:

```text
title, summary
findings[]: statement, status: OBSERVED | UNVERIFIED, evidence_refs[]
recommendations[]:
  title, description, stage, priority, owner?
  check, failure_condition, verification_plan
  validation_status: NOT_RUN | PASSED | FAILED
  evidence_refs[]
limitations[]
```

CI/schema lint and DB contract checks are proposals unless actual configuration
and test evidence establishes otherwise. Missing CI source is not proof of absent
controls. Operations follows the root result/failure; it never waits for execution.

## Read and approval APIs

`GET /api/analysis-parts/:id?engine=...` returns server-bound records and verified
payloads for all three parts. Expired/missing/unreadable bodies are explicitly
unavailable and do not hide other readable parts. No part record means legacy;
an existing invalid/revoked new part must not fall back to an old public runbook.

The playbook endpoint may return a READY recovery playbook during active analysis.
Its view also contains `source_mode: recovery`, `recovery_revision` and
`recovery_payload_sha256`; these are view metadata, not additions to the signed
playbook bytes.

Early approval requires client `expectedRecoveryRevision` plus the existing
`expectedPlaybookDigest`. The server re-reads the current part, verifies S3 identity
and hash, validates the full plan and actual ECS preconditions, saves the immutable
approval snapshot, then atomically checks:

- same current recovery revision and payload hash;
- COMPLETED + READY and not expired/revoked;
- same canonical runbook digest;
- parent identity/engine, no deletion, no analysis cancellation/outdating;
- no existing execution or active execution reservation.

The execution and `EXEC_ACTIVE` reservations share this transaction. A root/ops
failure alone does not invalidate a valid recovery part. Analysis cancellation
blocks new approvals; an already approved execution retains its own cancellation,
claim and deadline checks.

Record `source_part`, `source_part_revision`, `source_part_payload_sha256` on the
execution for display. Queue/worker authority remains the approved snapshot and
existing execution reservation; the worker must not reload a latest analysis part.
Same approval ID retransmission uses the original snapshot even if analysis changes.

Before the reservation transaction, copy the exact verified frozen incident bytes
to `approvals/<rca_id>/<approval_id>/incident.json`, alongside the immutable approved
playbook. Use create-only storage and verify identical bytes on replay. Do not
reserialize the incident or change its SHA-256 or originating engine.
Early reservations pin `source_alarm_name`, this approval-owned
`source_incident_s3_key`, `source_incident_sha256`, and `source_incident_engine`.
Optional `original_incident_s3_key` is tracing metadata, never a read fallback.
The origin engine may differ from the execution's analysis engine.
Do not duplicate the original alarm JSON in DynamoDB. The worker reads the exact
reserved approval copy from the configured evidence bucket and verifies its key,
SHA-256, schema, RCA identity, and origin engine before using its complete `alarm`.
The original alarm name must match the reserved name and approved metric wait.
Unknown fields, full Trigger, and AlarmDescription remain intact. Missing or invalid
references fail closed; neither the latest parent/part nor a legacy inline alarm
field substitutes for this source. UUID replay preserves these reserved references.
Analysis deletion or expiry must not remove the approval-owned copy or prevent
an already approved execution from reading it.

## Display and sequencing

Display a common incident header and three separately updating parts. Recovery
availability is independent of root confirmation. Execution status is separately
fetched; RESOLVED must not disappear behind a later RUNNING/FAILED analysis label.
Do not stop part polling because execution ended. Do not silently swap a runbook
being reviewed when its revision changes.

Recovery publication (including UNAVAILABLE/failure) precedes the existing RCA
tree/report work. Root work precedes operations. Existing RCA/Report helper calls
may remain internal to root_cause; they do not create a fourth visible part.
Each subsequent role starts only after its prior result or explicit failure is
stored. Shared deadline, cancellation, ownership and lost-source checks still
apply. No stage restarts the analysis time budget.

The existing RESOLVED-only execution retrospective and VERIFIED promotion remain
separate. If an early private runbook lacks a public library base, no unchecked
public update or promotion is allowed; preserve that follow-up outcome without
undoing recovery or blocking root/operations.
