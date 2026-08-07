#!/usr/bin/env bash
#
# Bundle the ADRs into 5 Markdown files for an AWS Security Agent design review.
#
# The design review UI accepts at most 5 files, 2MB each, 6MB total, so the 27
# ADRs are merged by the security boundary they describe rather than by folder.
# Deprecated and Superseded ADRs are left out: they do not describe the system
# as it stands and would give the reviewer a false picture of it.
#
# Output goes to .security-agent/design-review/ (gitignored).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADR_DIR="$REPO_ROOT/docs/adr"
OUT_DIR="$REPO_ROOT/.security-agent/design-review"

# Each bundle: <output-name>|<title>|<why these belong together>|<adr paths…>
BUNDLES=(
  "1-execution-and-permission-boundary|Playbook Execution and Permission Boundary|The write-capable execution path: how a playbook is produced, approved, executed, and revised, and which actions are refused.|agent/0017-playbook-execution-agent.md agent/0008-playbook-generation.md agent/0018-playbook-retrospective.md agent/0007-rca-report-generation.md infra/0008-playbook-execution-stack.md"
  "2-compute-isolation-and-trust|Compute Isolation and Trust Boundaries|Which workloads run read-only and which hold write permissions, and how the demo fault-injection environment is fenced off from them.|infra/0003-lambda-cc-headless-stack.md infra/0004-rds-healthcare-deployment.md infra/0007-demo-symptom-alarm-and-deployment-fault-injection.md agent/0011-cc-headless-prompt-driven-rca.md"
  "3-ingestion-storage-and-ownership|Alarm Ingestion, Evidence Storage and Session Ownership|What is allowed to start a session, where evidence is retained, and the ownership rules that decide who may claim, cancel, or delete one.|infra/0001-alarm-ingestion-sns-sqs-fargate.md infra/0002-evidence-storage.md infra/0005-execution-trace-dynamodb.md infra/0006-session-recovery-on-restart.md"
  "4-cross-account-and-tool-access|Cross-Account Access and External Tool Credentials|How the system reaches into other AWS accounts and what credentials the observability and repository tools receive.|infra/0009-multi-account-rca-hub-spoke.md tools/0006-cross-account-tool-credentials.md tools/0001-metrics-collection.md tools/0002-log-search.md tools/0004-deploy-history.md tools/0005-code-change-analysis.md"
  "5-analysis-engine-and-data-handling|Analysis Engine, Context Isolation and Notification|How untrusted incident data flows through hypothesis search, what context is isolated between steps, and what leaves the system as a notification.|agent/0001-initial-scoping-and-report-similarity.md agent/0002-hypothesis-tree-lifecycle.md agent/0006-termination-conditions.md agent/0014-hierarchical-evidence-session-isolation.md agent/0015-hexagonal-architecture.md agent/0016-rca-evaluation-test-harness.md agent/0009-notification.md agent/0010-model-tier-architecture.md"
)

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

for bundle in "${BUNDLES[@]}"; do
  IFS='|' read -r name title rationale adrs <<<"$bundle"
  out="$OUT_DIR/$name.md"

  {
    printf '# %s\n\n' "$title"
    printf '%s\n\n' "$rationale"
    printf 'Architecture decision records for the RCA Agent, an automated'
    printf ' root-cause-analysis system that reacts to CloudWatch alarms and can'
    printf ' execute remediation playbooks after human approval. Each section'
    printf ' below is one decision record, reproduced verbatim.\n\n'
    printf -- '---\n\n'

    for adr in $adrs; do
      if [[ ! -f "$ADR_DIR/$adr" ]]; then
        printf 'missing ADR: %s\n' "$adr" >&2
        exit 1
      fi
      printf '## Decision record: %s\n\n' "$adr"
      cat "$ADR_DIR/$adr"
      printf '\n\n---\n\n'
    done
  } >"$out"

  bytes=$(wc -c <"$out" | tr -d ' ')
  count=$(wc -w <<<"$adrs" | tr -d ' ')
  printf '%-40s %2d ADRs  %6s bytes\n' "$name.md" "$count" "$bytes"
done

total=$(find "$OUT_DIR" -name '*.md' -exec cat {} + | wc -c | tr -d ' ')
printf '\n%s\n' "$OUT_DIR"
printf 'files: %s   total: %s bytes (limit: 5 files, 2MB each, 6MB total)\n' \
  "$(find "$OUT_DIR" -name '*.md' | wc -l | tr -d ' ')" "$total"
