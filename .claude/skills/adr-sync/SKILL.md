---
name: adr-sync
description: Check synchronization between current codebase and related ADRs, then update ADRs and derived documentation (README, AGENTS, architecture, ops guides) if needed. Scans actual code to verify ADR decisions still hold. Also detects contradictions across related ADRs and propagates ADR updates to downstream docs. Examples - "/adr-sync", "/adr-sync agent", "/adr-sync infra"
---

# adr-sync

Review whether ADRs accurately describe the current codebase. Fix drifted ADRs, detect contradictions across related ADRs, and **propagate ADR changes to derived documentation** (root README, AGENTS, architecture docs, operations guides).

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

### 5. Downstream Documentation Propagation

After ADRs and cross-ADR contradictions are resolved, propagate the changes to **derived documentation** that summarizes or references ADR decisions. ADRs are the source of truth, but these documents must stay consistent.

#### 5.1. Scope of downstream docs

Check the following files for any content that references the updated ADR's decisions:

**Root-level**
- `README.md` — 프로젝트 개요, 패키지 표, 주요 기능, 환경 변수 표
- `AGENTS.md` — Repository Structure 표, Architecture at a Glance

**`docs/`**
- `docs/architecture.md` — Dual-Stack 비교 표, System Architecture 다이어그램, Technology Stack
- `docs/architecture-and-demo-flow.md` — 파이프라인 다이어그램, 단계별 데이터 흐름, 엔진 비교 표, 시퀀스 다이어그램
- `docs/system-guide-for-ops.md` — 전체 아키텍처 다이어그램, 엔진 비교, 파이프라인 단계 설명, 세션 상태 전이, 트러블슈팅

**Package-level (if updating category-specific ADRs)**
- `packages/<pkg>/AGENTS.md` — 해당 패키지의 아키텍처 개요 / 주요 결정

#### 5.2. Detection strategy

For each updated ADR in Pass 2, identify keywords/claims that likely appear in downstream docs:
- Model names and tiers (e.g., "Haiku 4.5", "2-tier", "Sonnet 4.6")
- Pipeline stage counts (e.g., "12단계", "9단계", "F1~F12")
- Environment variables (e.g., `BEDROCK_HAIKU_MODEL_ID`)
- State transition names, status values
- Architecture patterns or component names

Use grep to locate these keywords across the downstream doc set:

```bash
grep -rn "<keyword>" README.md AGENTS.md docs/ packages/*/AGENTS.md 2>/dev/null
```

#### 5.3. Update rules

- **Mirror the ADR decision**: Downstream docs should state the same fact as the updated ADR; never contradict it.
- **Update diagrams**: mermaid/tables that visualize the drifted concept must be updated, not just prose.
- **Preserve downstream voice**: downstream docs are typically more concrete (e.g., ops guide explains *what to do*). Keep tone/level but fix the facts.
- **Cross-reference the ADR**: when a claim in downstream docs has a nuanced update, add a short "(ADR <category>/<NNNN>)" pointer so readers can trace the decision.
- **Keep tables synchronized**: if the ADR's comparison table changes, mirror the change in every downstream comparison table.
- **Remove dead configuration**: if the ADR removes env vars / flags, remove them from downstream env tables and troubleshooting sections too.

#### 5.4. Final sweep

After editing downstream docs, run a final grep across the entire doc tree for the drifted keywords to confirm nothing was missed:

```bash
grep -rn "<removed_keyword>|<old_phrase>" docs/ README.md AGENTS.md packages/*/AGENTS.md 2>/dev/null
```

Any remaining hits should either be intentional (historical context, explicitly marked as "legacy") or fixed.

### 6. Report

After all fixes are applied and confirmed:

```
## ADR Sync Results

### Fixed (ADR 내용 갱신)
- [ADR 0006: ...] - updated section X to reflect Y

### Contradictions Resolved
- [ADR 0005 ↔ 0006] - description of what was inconsistent and how it was fixed

### Downstream Docs Updated
- README.md - <what changed>
- docs/architecture.md - <what changed>
- docs/system-guide-for-ops.md - <what changed>
- ...

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
- `docs/adr/README.md` Key Decisions must always be kept in sync when ADRs change — this is the primary index for future syncs
- **ADRs are the source of truth**: if a downstream doc conflicts with an updated ADR, the downstream doc is wrong and must be updated — never edit the ADR to match outdated downstream text.
- Derived documentation drift is common: downstream docs (ops guides, architecture overviews) tend to repeat details from ADRs. Always sweep downstream docs after any non-trivial ADR update.
