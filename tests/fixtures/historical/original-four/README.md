# Original four — historical inputs and normalized snapshots

Retired 2026-09-10. These files are preserved byte-for-byte. They are historical
inputs, not the active realistic catalog and not results for its replacements.
Existing snapshot passes do not turn later model failures into passes.

The failed/partial live run `tests/results/model/efficiency-20260909T145305Z-b05412`
and its reports remain untouched at their original paths. No live result was
copied, repaired, relabeled or approved by this migration. The original baseline
at `tests/baseline/rca-evaluation.json` remains unchanged and is stale for the new
catalog. No new model result snapshots have been manufactured.

Historical scenarios retain their original executionModes as provenance, not
as a current deployment capability declaration. Current capabilities belong to
`tests/scenarios/`. Tests may explicitly load this archive to exercise existing
evaluator behavior; the default model catalog never loads these files.

| Original path relative to tests | Archive path | SHA-256 |
|---|---|---|
| `scenarios/deployed-connection-leak-vital-ingest.json` | `scenarios/deployed-connection-leak-vital-ingest.json` | `9c6439168ca4990e0121b7545b3f9bc737791e85b11d12a52cdd3f36d4eaed7b` |
| `scenarios/deployment-query-regression.json` | `scenarios/deployment-query-regression.json` | `8bc4c9fe237500aa4652b3f314dff2351f0b62e7a8bc1cd16bc822b448d3be88` |
| `scenarios/iam-policy-access-denied.json` | `scenarios/iam-policy-access-denied.json` | `e64e3136878c1ee9bd571578dc9fd867ae1c87328a7bffc17c3bc4d59b3f9e6a` |
| `scenarios/rds-connection-pool-exhaustion.json` | `scenarios/rds-connection-pool-exhaustion.json` | `0924974eab44468d106080e21e6f60135c79bd2a5996a7dffc6f07db97cf7ac2` |
| `fixtures/results/headless-codex/deployed-connection-leak-vital-ingest.json` | `results/headless-codex/deployed-connection-leak-vital-ingest.json` | `85d49ccbf357c5c4081c858205ddb8ed8db053bca4e2acaefbc0da8ace481b52` |
| `fixtures/results/headless-codex/deployment-query-regression.json` | `results/headless-codex/deployment-query-regression.json` | `8797e48f7f0440abb44c0f95c5f027c5d541df9f395b553a70ce4464b1e35d00` |
| `fixtures/results/headless-codex/iam-policy-access-denied.json` | `results/headless-codex/iam-policy-access-denied.json` | `267eb50a7b8ea6545bd6a5c2edfa70f05613f0542b5a3145f5ecb405d716a497` |
| `fixtures/results/headless-codex/rds-connection-pool-exhaustion.json` | `results/headless-codex/rds-connection-pool-exhaustion.json` | `ce778a6bae127afd793c8709032490b879482dca3017cc4dc97b0b9bd37444fa` |
| `fixtures/results/strands/deployed-connection-leak-vital-ingest.json` | `results/strands/deployed-connection-leak-vital-ingest.json` | `6f36aaec5595d9bfcd051d6fb15ec176c40f845aaa56a4d079ac4332a67b8a7c` |
| `fixtures/results/strands/deployment-query-regression.json` | `results/strands/deployment-query-regression.json` | `147b534411d2aec2cd0b4ce29789649fa7f815aa8001f7b29c7c2dd458efa0c6` |
| `fixtures/results/strands/iam-policy-access-denied.json` | `results/strands/iam-policy-access-denied.json` | `d92f3300de2e145ad0636da06616705a2020ed189abaa037040b9aabcadc47e0` |
| `fixtures/results/strands/rds-connection-pool-exhaustion.json` | `results/strands/rds-connection-pool-exhaustion.json` | `ce74a30a17f01aa541712dd00bbc66129f4b4c148e554902b3ce9f34b7e023f5` |
