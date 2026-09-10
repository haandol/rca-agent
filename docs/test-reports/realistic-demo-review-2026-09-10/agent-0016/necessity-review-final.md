# Necessity Review — Final N1 Recheck

## Verdict

**PASS for necessity. N1 is closed.** The fix removes the unnecessary arbitrary-program extension while retaining the original owned maintenance and restoration contract. No new blocking necessity finding was found in this narrow recheck.

| ADR | Final necessity result | Disposition |
| --- | --- | --- |
| `docs/adr/infra/0004-rds-healthcare-deployment.md` | PASS | Original necessity conclusion retained. N2 remains optional and unchanged to preserve measured service source. |
| `docs/adr/infra/0007-demo-symptom-alarm-and-deployment-fault-injection.md` | PASS | N1 verified fixed: fixed maintenance command; obsolete arbitrary launches refused; owned restoration remains available. |
| `docs/adr/agent/0016-rca-evaluation-test-harness.md` | PASS | Original necessity conclusion retained; no observed changes to model inputs, adapters, evaluator policy or baseline. |

This updates only N1's disposition in the preserved `necessity-review.md`. That report remains the authority for the original full callscope, 26 contract rows, independent grounding and previous validation limits. PASS here is not ADR promotion, a sufficiency verdict, baseline approval, model-quality validation, or AWS E2E success.

## Scope and original contract

Read only the changed CLI and relevant tests, the original counterexample, the previous fingerprint manifest, and the relevant original ADR contract. No sufficiency/refactor reports or explanation documents were read.

The same original obligations apply:

- infra/0007 R3: the maintenance transaction must hold a real write-conflicting lock, have an owned run identifier and finite hold bound, clean up on failure/signal, and never terminate another run's work.
- infra/0007 R8: preserve original state before changes; attempt all owned restoration steps after failure/interruption; judge recovery by restored service symptoms.
- infra/0004 R3/R5: use the actual owned maintenance mechanism and clean up its own work and connections.

The ADRs require that behavior, not a caller-defined executable. Removing the arbitrary argv selector is therefore appropriate. The small validation helper is necessary at the persisted-journal boundary: removing the CLI flag alone would leave old arbitrary commands executable. Keeping restore independent from launch validation prevents old journal contents from stranding an owned task.

Fingerprint comparison against the original review found exactly four changed recorded files:

- `scripts/run_realistic_demo.py`
- `tests/harness/realistic-demo-script.test.mjs`
- `tests/harness/realistic_demo_cases.py`
- `scripts/run_realistic_demo.freeze.json`

All previously fingerprinted ADR/mapping, service source, model-input/projection, adapter, evaluator and baseline files match the original review. The freeze file's test claims were not used as verification evidence.

## N1 closure evidence and callscope

| Unit | Current evidence | Necessity judgment |
| --- | --- | --- |
| CLI parsing | `scripts/run_realistic_demo.py:1266` no longer registers `--maintenance-command-json`; `main:1385` parses before constructing the AWS adapter. New plan options contain no caller command. | Required narrowing; the former option has no retained launch path. |
| Fixed command builder | `maintenance_command:131` compares any legacy `maintenance_command` to the fixed constant, validates run ID, finite bounded duration and schema, then formats **`MAINTENANCE_COMMAND`**, not the journal command. | Required small boundary check, not a replacement generic command framework. |
| Registration | `Demo.register:654` calls the helper at 665; entrypoint and command are slices of its fixed result. It preserves the original healthy image digest and isolated maintenance task construction. | Same owned mechanism; arbitrary executable selection removed. |
| Apply guard | `Demo.apply:740` checks a maintenance journal at 746 before service lookup, claim/tagging, apply intent or task registration. Direct maintenance registration also checks independently. | Necessary to reject old persisted arbitrary launch instructions before side effects. |
| Restore | `Demo.restore:1169` → `restore_once:1192` → existing owner-checked task discovery/stop and symptom verification. It does not invoke the launch command helper. | Necessary compatibility: an old command cannot authorize a new launch or prevent cleanup of an already-owned task. |
| Focused tests | `realistic_demo_cases.py:536` rejects old apply/register but preserves lost-response cleanup; `:581` checks invalid persisted values. Node CLI tests reject the removed option for all four actions. | Relevant contract tests; no unnecessary product behavior or new dependency introduced. |

## Independent counterexample rerun

Executed `necessity-reproduce-final.py`, saved beside this report. It uses the existing in-memory `Cloud` fake and an AWS-constructor tripwire; it never invokes AWS or executes the supplied program.

1. **Former CLI counterexample:** supplied `["python", "-c", "pass", "{run_id}", "{hold_seconds}"]` through the removed flag for plan/apply/status/restore. All four exited **2** with “unrecognized arguments: --maintenance-command-json”; **zero AWS constructor calls**.
2. **New normal launch:** a plan without a command override produced exactly:

   ```text
   python -m test_service.maintenance --run-id run-1 --hold-seconds 179.5 --schema owned_schema
   ```

   The original healthy image digest remained pinned. The plan did not serialize an arbitrary command.
3. **Old arbitrary journal:** recreated a valid hash-linked snapshot containing the original arbitrary command. Both `apply()` and direct `register(maintenance=True)` rejected it with “unsupported maintenance command”; **zero fake AWS calls**, no apply intent and no registration intent.
4. **Restore that old journal:** added an already-owned task's launch intent and ownership markers, deliberately without a recorded RunTask response. Restore rediscovered and stopped **only `arn:task/owned`**, left the foreign task running, and made **no RegisterTaskDefinition or RunTask call**. Initial recovery remained pending; after fresh simulated metric periods it verified recovery. Original snapshot bytes were unchanged.
5. **Exact fixed legacy command:** a legacy field equal to `MAINTENANCE_COMMAND` remained accepted; its values are rendered from validated run/hold/schema settings.

Result: **the original N1 counterexample is blocked and the required recovery behavior survives.** Reproduction output is `necessity-reproduction-final.json`.

## Executed verification

| Command | Actual result |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 python3 /private/tmp/rca-realistic-review/necessity-reproduce-final.py` | Passed all independent assertions; no external AWS/model calls. |
| `PYTHONDONTWRITEBYTECODE=1 python3 tests/harness/realistic_demo_cases.py -v` | **40 tests passed**, 2.676s. Log: `necessity-python-final.log`. |
| `PYTHONDONTWRITEBYTECODE=1 node --test tests/harness/realistic-demo-script.test.mjs tests/harness/realistic-scenarios.test.mjs` | **15 tests passed**, 10.863s. Includes the Python lifecycle wrapper and incident-projection checks. Log: `necessity-root-final.log`. |

The worker-reported 46-root-test result is not claimed as independently rerun here. This pass deliberately reran the 15 directly relevant root tests and the 40 Python cases. The Python wrapper rerun is not an additional distinct suite.

## Preserved findings and limits

- **N2 stays optional and unchanged.** The unused private ingest wrapper is not a release blocker, and its removal is not required to close N1. Current service-source hashes match the original measured source.
- The original strict baseline digest failure was not rerun or resolved here. No baseline approval or model result is inferred from this patch.
- AWS deployed E2E and models were **not run**. Simulated recovery is only offline evidence; it does not establish live alarm transitions, deployed ownership behavior or real recovery causality.
- No repository code, ADR, mapping, baseline or test was edited by this reviewer. Original report and original reproduction artifacts were preserved. No browser was opened.
- Exact final hashes and the original report's preservation hash are in `necessity-final-fingerprints.json`. CLI SHA-256: `5562cb41c16ee202bd24506a9b130868d432d7e2eeb89854eb25d29009cf7e85`.
