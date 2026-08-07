# DESIGN.md

Machine-readable design tokens for the RCA Agent dashboard. AI agents should read this
file first for all UI work.

For implementation details (data access, page structure, common mistakes), see
[AGENTS.md](./AGENTS.md).

## Metadata

- **App**: RCA Agent dashboard — the reading surface for automated root-cause analysis
  sessions, their causal chains, and the playbooks a person approves for execution
- **Stack**: Nuxt 4 (TypeScript) + TailwindCSS 4 + DaisyUI 5 (two custom themes, no stock theme)
- **Theme**: light only (`workflow`) | **Font**: Crimson Pro (display) + Inter (UI), self-hosted

## The register

The dashboard reads as an **editorial manuscript**, not a monitoring console. A page of
sessions is an archive of things that already happened; the pages are read for minutes at
a time, and most of what is on them did not go wrong. Authority comes from typographic
restraint and whitespace, never from saturated color or elevation.

Two consequences an agent must respect:

- **A whisper-weight serif headline is the signature.** Crimson Pro at weight 300 is
  non-negotiable for headings. A bold serif or a sans heading breaks the system outright.
- **Color is near-zero.** One sage green, used only as functional punctuation. Every
  surface is white, off-white, or warm gray.

> **This register replaces the previous "ledger" theme** (paper ground + ember accent for
> live runs). The structural inventions of the ledger survive intact — the spine, the run
> bar, the causal chain — because they encode what an incident _is_; only the palette,
> type, and separation method change. Where the ledger separated surfaces by value alone,
> this system separates them with hairline borders.

## Colors

Ten tokens. There is no eleventh — introducing blue, red, or purple breaks the system.

| Name          | Value     | Role                                                                                                                               |
| ------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Ink Black     | `#1a1a1a` | Primary text, icon strokes, card and section borders, nav links                                                                    |
| Paper White   | `#ffffff` | Page canvas, card surfaces, button text on dark fills                                                                              |
| Graphite      | `#6a6a6a` | Secondary text, helper copy, muted borders                                                                                         |
| Warm Charcoal | `#4d3f32` | Warm-toned text and borders — the brown-tinted dark used where the system needs a softer, paper-friendly alternative to pure black |
| Fog           | `#f6f6f6` | Subtle section backgrounds, button hover fills, nav border, badge surfaces                                                         |
| Linen         | `#ececec` | Card surface tint, secondary background panels                                                                                     |
| Hairline      | `#e3e3e3` | Dividers and borders where a softer separator is needed                                                                            |
| Pebble        | `#8d8d8d` | Tertiary icon strokes, decorative borders                                                                                          |
| Sage          | `#547e69` | Green outline accent — tags, dividers, focused UI edges                                                                            |
| Mint Wash     | `#f1fcf6` | The only chromatic background; approval/positive status surface                                                                    |

### Sage is functional punctuation, not decoration

Sage marks **active state and approval** — the tab underline, the approve pill, a focused
edge. Never a fill, never a CTA background, never a decorative shape.

This dashboard has one action a person can actually take: **approving a playbook**. Sage
belongs there and to the live-run indicator, and nowhere else. Everything else on the page
is already over, and coloring finished work buries the one row still waiting on a human.

### State without color

The old palette spent four hues on state (ink / ember / rust / moss). This one cannot, so
state is carried by **form** instead:

| State                | How it reads                                                                              |
| -------------------- | ----------------------------------------------------------------------------------------- |
| Running              | Sage node, `.run-bar-open` dashed bar, `.animate-pulse-soft`                              |
| Pending approval     | Sage Status Pill (outline, `9999px`)                                                      |
| Completed            | Filled Ink Black node, solid run bar, no pill                                             |
| Failed / unresolved  | Ink Black node with a struck-through or hollow treatment, and the word — never a red chip |
| Skipped (`OUTDATED`) | Graphite text, hollow node                                                                |

**A failure must never be conveyed by color alone**, because there is no red in this
palette. Failed and unresolved states carry an explicit label. This is a hard constraint,
not a preference — an operator scanning for breakage has only the word and the form.

## Typography

Three faces. Latin-only — this build carries no Korean glyph coverage in the display or UI
faces (see the warning below).

### Crimson Pro — headlines and section titles

- **Weight**: 300 only. **Sizes**: 26px, 32px. **Line height**: 1.0.
- **Substitute**: Cormorant Garamond, Libre Caslon Text, or Playfair Display at 300.
- Weight 300 is the signature move. Most dashboards set headings at 600–700 sans; this
  one trades volume for editorial elegance.

### Inter — body, UI labels, nav, buttons, captions

- **Weights**: 400 for everything; **500 reserved** for button labels and active nav emphasis.
- **Sizes**: 11, 12, 13, 14, 15, 16px. **Line height**: 1.35–1.91.
  **Letter spacing**: −0.004em to 0.004em.

### Georgia — inline editorial body

- **Weight**: 400. **Size**: 13px. **Line height**: 1.35–1.62.
- Reserved for **report prose and the causal chain** — the article-preview register. A
  finding is an argument, and an argument is set in a serif. Never for UI chrome.

### Type scale

| Role       | Size | Line height | Letter spacing |
| ---------- | ---- | ----------- | -------------- |
| caption    | 11px | 1.4         | 0.04px         |
| body-lg    | 16px | 1.5         | —              |
| heading-sm | 26px | 1           | —              |
| heading    | 32px | 1           | —              |

### Numerals

Clock readings, RCA ids, durations, and counts are compared down a column, so they take
`tabular-nums`. This is inherited from the ledger and unchanged: a timestamp column with
proportional digits cannot be scanned.

> **⚠ Korean coverage is a known gap.** Crimson Pro and Inter carry no Hangul. Reports and
> causal chains in this system are written in Korean, so on this surface they fall back to
> whatever the browser supplies — which is not a designed face and will not match the
> Latin metrics. This was accepted deliberately to follow the reference style exactly. If
> mixed KO/EN typesetting quality becomes a problem, the fix is a Hangul companion in each
> stack (a serif beside Crimson Pro, a sans beside Inter), not a switch away from weight 300.

## Spacing & Layout

**Base unit**: 4px. **Density**: comfortable.

- **Page max-width**: 1080px — content centered in a single column, no sidebars, no
  asymmetric composition
- **Section gap**: 80px
- **Card padding**: 24px
- **Element gap**: 16px

Whitespace carries the layout. Reach for a divider or a background only when spacing has
already failed.

### Border radius

Four values exist. Do not introduce a fifth, and do not use `0px`.

| Target        | Radius |
| ------------- | ------ |
| cards, images | 12px   |
| buttons       | 8px    |
| badges        | 4px    |
| pills         | 9999px |

> This is the sharpest break from the ledger, which lived at 2–4px (`--radius-box: 0.25rem`).
> The theme block's `--radius-*` tokens must be updated together with this table; a mix of
> old and new radii on one screen reads as two products.

### Separation

**Hairline borders are the primary method** — `1px` in Hairline (`#e3e3e3`) or Fog
(`#f6f6f6`). Shadow is reserved for the one floating surface (see Elevation).

> This **inverts the ledger's rule**, which forbade borders on `.sheet` on the grounds that
> a page of bordered boxes reads as a form. The editorial register accepts that risk in
> exchange for structure without color: with the hue budget at near-zero, the border is the
> only tool left to say where one thing ends. Keep borders at `1px` and never stack a
> border with a shadow on the same resting element — that is what actually produces the
> boxed-form feel.

## Elevation

Two shadows exist in the entire system. Everything else is flat.

- **Product preview / floating card**: `rgba(0,0,0,0.06) 0px 2px 6px 0px`
- **Hero panel**: `rgba(0,0,0,0.03) 0px 1px 3px 0px, rgba(0,0,0,0.03) 0px 5px 5px 0px, rgba(0,0,0,0.02) 0px 10px 6px 0px`

Elevation stays under `0.06` alpha. Resting cards get a border, never a shadow.

## Surfaces

- **Paper White** (`#ffffff`) — primary canvas; every section opens on this
- **Fog** (`#f6f6f6`) — quiet section alternation, button hover fills
- **Linen** (`#ececec`) — card tinting where a card must read as lifted off the page
- **Mint Wash** (`#f1fcf6`) — approval/positive status only; the sole chromatic background

## Components

### Primary Text Button (ghost)

**Role**: main CTA.

No fill, Ink Black text in Inter 500 at 14–16px, `8px` radius, `8px 16px` padding, no
border. The type weight and the arrow glyph carry the affordance.

### Outlined Action Button

**Role**: secondary action.

White fill, `1px` Ink Black border, Inter 500 14px, `8px` radius, `12px 20px` padding.

### Pill Badge

**Role**: compact metadata — engine name, session count, step ordinal.

Fog background, Graphite text at 12–13px Inter 400, `4px` radius, `4px 8px` padding.

### Sage Status Pill

**Role**: approval / active / positive state.

White fill, `1px` Sage border, Sage text at 13px Inter 500, `9999px` radius. This is the
pill for **승인 대기** and a live run — the two things a person acts on.

### Feature Card

**Role**: session summary, report block, retrospective panel.

White surface, `1px` Hairline border, `12px` radius, `24px` padding. Headline in Crimson
Pro 300 26px Ink Black; body in Inter 400 15px Graphite.

### Tab Bar

**Role**: view switcher.

No background. Tabs in Inter 400 14px, `16px` horizontal / `8px` vertical padding. Active:
Ink Black text with a **Sage 2px underline**. Inactive: Graphite, no underline.

### Navigation Bar

White background, `1px` Fog bottom border. Wordmark in Inter 500 16px Ink Black; links in
Inter 400 14px, `16px` gap. Flat horizontal list, no dropdown chrome.

### Share/Approve Action Row

**Role**: top-right actions on a report or playbook surface.

Inline group: a ghost text button, then an **outlined Sage pill** for approve (`9999px`,
`6px 12px`). No filled background button — shape and border carry the weight.

> The approve action is gated server-side (analysis complete, confirmed cause, valid
> procedure, no execution in flight). Never render the pill as available when those
> conditions do not hold — see AGENTS.md. A pill the server will reject with 409 makes the
> approval gate meaningless.

## Structural signatures

These are this product's own inventions, carried over from the ledger unchanged in form.
They encode what an incident is, so they survive a restyle.

### The spine

One continuous vertical rule down the left of the archive, with every session hung off it
at its own hour. Time is the axis the page is built on, not a column at the far right —
the whole finding in a report is that a deploy at 05:40:02 preceded a spike at 05:40:00.

- The rule is a `1px` line in Hairline; nodes are `9px` circles
- A **day label sits across the rule**, never in the time gutter — a date and a clock
  reading in the same column read as one malformed timestamp
- Clock readings sit in the gutter in `tabular-nums`

### The run bar

Duration as a **length**, not a number: width computed inline from the real elapsed time,
so a 42-minute run is visibly four times a 10-minute one. A minute count must be compared
arithmetically; a bar is compared by eye. The longest run on screen sets the scale. An
open (still-running) bar is dashed.

### The causal chain

The finding set as a linked descent — each answer becomes the next question. The search
runs in parallel and most branches are discarded, but **the result is linear**, so the
report page leads with the chain and leaves the parallel DAG to the trace page.

- Questions and answers in the serif (Georgia / Crimson Pro register), 15–16px
- A `1px` connecting rule makes it a descent rather than five stacked paragraphs
- The **last link is the one marked in Sage** — that is where the fix belongs
- Parsing is defensive: a half-parsed chain is worse than none, so on failure the page
  falls back to the full report prose

### Report prose

`.prose-report` at `max-w-[68ch]`. The generated H1 restates the RCA id already in the
header, so it is suppressed. Section headings step down to 13px uppercase with wide
tracking in Graphite; body at 16px / 1.72.

## Motion

- Transitions 120–200ms. No bounce, no spring.
- **A live run is the only thing on the page that moves.** Nothing else animates on a
  resting surface.
- `prefers-reduced-motion` collapses all animation and transition to ~0 and keeps meaning
  in static form.

## Do / Don't

**Do**: Crimson Pro **300** for every headline · Inter 400 for body and UI, 500 only for
button labels and active nav · surfaces limited to Paper White, Fog, Linen · Sage only for
active underlines, approval pills, and functional icon accents · radii at 4 / 8 / 12 /
9999px · `1px` hairline borders as the separation method · 1080px centered column with
80px section gaps · `tabular-nums` on every clock reading, id, and count · an explicit
label on every failure state · both `aria-label` and visible text on any status conveyed
by shape

**Don't**: a filled colored button as primary CTA (CTAs are ghost text or outlined pills) ·
any hue outside the ten tokens — no blue, red, or purple · bold or semibold headlines
(Crimson Pro 300 is non-negotiable) · shadows over `0.06` alpha, colored glows, or a
shadow on a resting card · `0px` or `16px+` radius on cards · color as the only carrier of
a failed state (there is no red here) · Sage as a fill or CTA background · Georgia for UI
chrome (report and chain prose only) · hardcoded hex in page styles — read the theme
tokens, so a palette change lands everywhere at once · a second thing on the page
animating beside a live run · storage-layer vocabulary in state labels (`OUTDATED` is a
skipped-analysis verdict, not a TTL expiry)
