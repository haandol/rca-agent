---
name: adr-sync
description: Check synchronization between current codebase and related ADRs, then update if needed. Scans actual code to verify ADR decisions still hold. Also detects contradictions across related ADRs. Examples - "/adr-sync", "/adr-sync agent", "/adr-sync infra"
---

# adr-sync

Review whether ADRs accurately describe the current codebase. Fix drifted ADRs and detect contradictions across related ADRs.

## Workflow

### 1. Determine Target & Read README Index

If the user specifies a category (e.g., `/adr-sync agent`), target that. Otherwise, target all categories.

Read `docs/adr/README.md` — this is the **lightweight index** containing Key Decisions for every ADR. This is the only file you read in Pass 1.

Category → code mapping (for grep verification in Pass 1):
- agent: `packages/agent/` — 가설 생성기, 오케스트레이터, 상태 머신, 가설-트리 탐색
- tools: `packages/tools/` — MCP 도구, CloudWatch, Logs, X-Ray, CloudTrail, S3 Vectors 도구
- infra: `packages/infra/` — CDK 스택, ECS Fargate, SNS/SQS, DynamoDB, S3, VPC
- web: `packages/web/` — RCA 대시보드, 가설 트리 시각화, 증거 패널, 보고서 뷰

### 2. Pass 1 — Lightweight Drift Detection (README + grep only)

For each ADR in the target categories, use the Key Decisions summary from README.md to verify against the codebase **using grep only** (do NOT read full files):

- Extract key architectural claims from the summary (e.g., "Supervisor-Orchestrator 패턴", "DynamoDB 상태 머신", "S3 Vectors 유사도 검색")
- For each claim, run targeted grep on the category's code paths to verify it still holds
- Mark each ADR as **In Sync** or **Drift Suspected**

**Token budget rule**: Pass 1 should consume only README.md (~200 lines) + grep results. Do NOT read any ADR files or full source files in this pass.

### 3. Pass 2 — Fix Drifted ADRs (full read only for drift)

For ADRs marked **Drift Suspected** in Pass 1:

1. Read the full ADR file
2. Read relevant source code to understand the current implementation
3. **Make the corrections directly** — edit the ADR to match the current codebase
4. **Update README.md Key Decisions** to reflect the change
5. Present the diff to the user for confirmation

When updating ADRs:
- Also update the `docs/adr/README.md` index entry
- Do NOT add implementation details (file paths, code snippets, DB field schemas, constants) — see `docs/adr/README.md` Writing Rules
- If the ADR already contains such details, remove them during the update

### 4. Cross-ADR Contradiction Check

After drift fixes, check for **contradictions across related ADRs**:

1. For each updated ADR, read its `## Related` section to find linked ADRs
2. Read the **README.md Key Decisions** of each related ADR (not the full file)
3. Check for contradictions:
   - Does ADR A say "X is required" while ADR B says "X is removed"?
   - Do two ADRs describe the same feature with conflicting behavior?
   - Does a Superseded ADR's decision still appear as active in a related ADR?
4. If contradictions found → read the full related ADR → fix → present to user

**Common contradiction patterns**:
- Feature marked Superseded but still referenced as current elsewhere
- Conflicting configuration values or behavior rules across ADRs
- Status mismatch (Proposed vs Accepted) for implemented features

### 5. Report

After all fixes are applied and confirmed:

```
## ADR Sync Results

### Fixed
- [ADR 0006: ...] - updated section X to reflect Y

### Contradictions Resolved
- [ADR 0005 ↔ 0006] - description of what was inconsistent and how it was fixed

### In Sync (no changes needed)
- [ADR 0001: ...], [ADR 0002: ...], ...

### Suggestions
- [New ADR needed?] - description if new architectural decision found
- [README Key Decisions update?] - if summaries were stale
```

## Notes

- ADRs document "why this decision was made" — focus on architectural decisions, not implementation details
- Minor bug fixes or style changes do not warrant ADR updates
- ADR numbers increment sequentially within each category
- README.md Key Decisions must always be kept in sync when ADRs change — this is the primary index for future syncs
