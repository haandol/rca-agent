# F4 missing alarm metadata — applied and verified

Applied after the explicit RELEASE message. No repository changes were made before release. The patch is relative to the existing dirty working tree, not HEAD; it preserves other workers' edits. `f4.patch` is the exact applied delta and must not be reapplied to the already-fixed tree.

## Behavior

- Both eval adapters attach an explicit source metadata view only for `model-eval`. Missing region is `not provided`, regardless of AWS runtime/credential region. No ARN is synthesized. Supplied ARN stays in source metadata; Strands also retains its supplied ARN field. A supplied region without ARN reaches the actual prompt.
- Actual Strands scoping and Headless orchestrator/RCA/report prompts render only provided metadata. Omitted/null/blank fields display `not provided`; supplied zero and explicit empty dimensions remain zero and `{}`.
- Strands keeps its fresh session timestamp for idempotency and staleness handling, but the model sees only the source state-change time, or `not provided` when absent.
- Production parsing/defaults remain in place. Strands optional statistic/period types also pass absence through the completion notification DTO without failing validation. Production values remain `Average` and `300`.
- Three production examples per engine (minimal, metric-only, complete with zero) rendered byte-for-byte identically before and after the patch. Headless comparisons cover all three roles. JSON prompt artifacts are in `f4-work/{strands,headless}-{before,after}-prompts.json`.
- The original F4 missing/region-only repro and four copied catalog cases demonstrate fabricated `us-east-1` before the fix and no fabricated/default region afterward. Final repository tests read the refreshed canonical catalog.

## Verification completed

| Check | Result |
| --- | --- |
| Strands repository metadata, DTO/prompt contract, parser, scoping, notification, eval pipeline contract | 113 passed |
| Headless repository metadata, adapter, citations, prompt builder/contracts, parser, pipeline | 201 passed |
| Ruff check on 9 affected Python files | Passed |
| Ruff format check on 9 affected Python files | Passed |
| git diff --check on all 10 affected files | Passed |
| Affected-file byte comparison against captured baseline before applying | Passed; no concurrent edit overwritten |
| Applied files compared against tested candidate | Passed |

Actual rendered-prompt tests cover omitted, null, blank and whitespace metadata; each provided field alone; region without ARN; provided ARN visibility; all fields together; period/threshold/evaluation/datapoint zero; absent namespace/time; empty dimensions; all four catalog cases; source observation preservation; exclusion of private answers; production defaults; and non-eval opt-out. No grader/scenario/limit/rejection/model-quality policy files changed.

Commands (from each package directory):

```sh
# packages/agent
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  tests/test_eval_alarm_metadata.py tests/test_harness_prompt_dto_contracts.py \
  tests/test_scoping.py tests/test_models.py tests/test_notification.py \
  tests/test_eval_adapter_pipeline_contract.py -q -p no:cacheprovider

# packages/headless-codex
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  tests/test_eval_alarm_metadata.py tests/test_eval_adapter.py tests/test_eval_citation_fields.py \
  tests/test_prompt_builder.py tests/test_prompt_contracts.py tests/test_alarm_parser.py \
  tests/test_pipeline.py -q -p no:cacheprovider
```

Logs: `f4-agent-repo-tests.log`, `f4-headless-repo-tests.log`. `f4-applied.json` records the patch and affected-file hashes. An initial Strands test invocation named a nonexistent `test_pipeline.py` and collected no tests; the corrected command above passed.

## Handoff

Repository fix and scoped offline verification are complete. Main may now freeze the final model input digest and run its separately authorized final evaluation. No AWS requests, model calls, key access, baseline approval, commits, or scenario/capture modifications were performed for F4. The interrupted diagnostic run remains separate evidence; these offline tests do not establish model-quality success.
