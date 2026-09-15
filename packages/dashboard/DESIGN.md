# RCA Dashboard Design System

The dashboard is an incident-operations console. It exists to help an operator answer
four questions quickly:

1. What still needs attention?
2. Which engine and run produced the result?
3. What evidence supports the root cause?
4. Is it safe to approve the remediation?

For data contracts and implementation constraints, see [AGENTS.md](./AGENTS.md).

## Product register

- **Visual model**: operations cockpit, not archive, blog, report cover, or editorial
  manuscript
- **Default theme**: dark (`rca-ops`), with an equivalent light theme
  (`rca-ops-light`)
- **Type**: Inter for UI and prose, JetBrains Mono for identifiers, timestamps,
  durations, states, and commands
- **Density**: compact enough to scan many incidents without hiding the root-cause
  summary
- **Hierarchy**: sidebar → page header → metric cards → controls → incident queue

The interface must make states distinguishable at a glance. Color is allowed and
expected, but visible text remains mandatory so color is never the only carrier.

## Color roles

Use DaisyUI semantic tokens instead of hard-coded page colors.

| Token          | Meaning                                                       |
| -------------- | ------------------------------------------------------------- |
| `primary`      | selected navigation, focus, links, primary inspection actions |
| `info`         | analysis currently running                                    |
| `warning`      | human approval or investigation required                      |
| `success`      | incident resolved or procedure verified                       |
| `error`        | failed, cancelled, or unresolved outcome                      |
| `base-100`     | panel and row surface                                         |
| `base-200`     | workspace canvas                                              |
| `base-300`     | raised control or selected surface                            |
| `base-content` | foreground text and borders                                   |

State mapping:

| Outcome     | Tone                                          |
| ----------- | --------------------------------------------- |
| 분석 중     | info / blue                                   |
| 승인 대기   | warning / amber                               |
| 해결        | success / green                               |
| 미해결      | error / red                                   |
| 원인 미확정 | warning / amber, lower emphasis than approval |
| 분석 중단   | error / red                                   |
| 건너뜀      | neutral / muted                               |

Every state presentation includes a Korean label. Failed and unresolved states must
not rely on hue alone.

## Typography

- Page title: Inter 700, 24–30px
- Section title: Inter 600, 14–18px
- Body: Inter 400, 13–15px
- Labels: Inter 600, 11–12px, optional uppercase tracking for English labels
- Data: JetBrains Mono 400–600, 10–13px, tabular numerals
- Long report prose stays sans-serif. The dashboard is a tool, not a publication.

Hangul uses the system sans fallback after Inter. Do not use a Latin-only display face
for Korean headings.

## Layout

- Desktop: 224px navigation rail and a fluid workspace
- Workspace maximum width: 1440px
- Main padding: 24–32px desktop, 16px mobile
- Primary grid: four metric cards, then a full-width incident queue
- Report summaries use compact panels; detailed content stays within one switching dialog
- Mobile: navigation becomes a top strip and incident rows stack vertically

The sidebar establishes product identity and keeps the screen from reading like a
standalone article. Do not add navigation items that have no implemented destination.

## Surfaces

- Workspace canvas uses `base-200`
- Cards and rows use `base-100`
- Selected or hover surfaces use `base-300`
- Borders are `1px` with 10–18% foreground opacity
- Radius is 8px for cards, 6px for controls, and full radius for status chips
- Use one subtle shadow only for sticky chrome or modal surfaces
- Avoid large empty white areas and decorative gradients behind body content

## Core components

### Metric card

Shows one operational count with a label, large tabular value, and one-line meaning.
The semantic color appears in a narrow top rule or small icon, not as a full saturated
background.

### Status chip

Compact rounded label with semantic border, text, and low-opacity fill. It always
contains the outcome label.

### Incident row

An incident row exposes, without hover:

- outcome
- alarm name
- root-cause or failure summary
- engine
- start time
- duration
- report action

Trace, playbook, cancel, and delete are secondary actions but remain keyboard
reachable. Destructive actions may be quieter until hover on pointer devices, but they
must remain visible on touch/mobile layouts.

### Duration bar

Duration remains encoded as both a number and a relative bar. The bar is supporting
information, not the primary row structure.

### Approval surface

Runbook approval authorizes commands against the incident's operational targets.
Use warning emphasis while a decision is pending, state exactly how many steps
will run, and keep the server-side gate explanation beside the control.

Knowledge proposal apply/reject controls use a separate labeled surface. They
change reusable knowledge, while runbook approval authorizes the incident's fixed
commands. Never combine their confirmation or success states.

### Report and evidence panels

Within the selected modal view, causal chain, timeline, execution plan, evidence
and execution history use separate bordered panels. Unselected content stays hidden.
Long generated Markdown uses a readable 72–80 character measure inside the panel.

The report starts with incident, impact, cause, a compact recorded 5 Whys chain,
next action, runbook readiness and generation references. Missing historical values
are shown as missing. Each short chain step opens its full evidence in the same
dialog; parsing failure offers the original report without inventing a chain. Details open
in one native dialog; changing the selected detail replaces content within that
dialog. Runbook review and approval remain inside it. Closing restores the
operator's focus and page scroll. Each data source has its own loading, failure
and retry surface.

Generation references list only recorded actual uses, with role, source reference,
RCA, engine and applicable revision. Open frozen reused knowledge, current incident
inputs and every observation matching a used citation in the same modal. Considered
search candidates are not generation references. When an older record lacks an
actual-use list, label its preserved baseline as a comparison record. Keep knowledge
update before/after, rationale and apply/reject controls in this reference panel.

The playbook library has its own sidebar destination. Its rows show type,
symptom, tags, update time and the linked runbook's verification state. Detail
opens in a dialog; pending publication and unavailable originals remain visibly
distinct from usable published knowledge.

### Graphs

Graphs use the same semantic state colors and panel background as the rest of the
console. Selected nodes receive a visible ring. Graph detail must remain readable
without interpreting edge color alone.

## Motion

- 120–180ms transitions
- Running state may pulse softly
- No decorative motion, bounce, or animated backgrounds
- Respect `prefers-reduced-motion`

## Accessibility and visibility

- Minimum body contrast follows WCAG AA in both themes
- Focus uses a 2px primary outline with offset
- State labels are visible text
- Buttons use a minimum 32px target; primary actions use 36px or larger
- Information hidden on hover must also appear on focus and always appear on touch
- Truncated summaries retain the full value in `title` where practical

## Do / Don't

Do:

- lead with pending decisions and active work
- use semantic colors consistently
- keep timestamps and identifiers monospaced
- use compact panels and clear column alignment
- show the root-cause summary in the queue
- keep report summaries and the library inside the app shell, with report details in one switching dialog

Don't:

- use serif display headings
- center the whole product in a narrow article column
- present incidents as a decorative timeline
- hide important state distinctions in gray-on-gray text
- make approval look equal to delete or cancel
- add unimplemented navigation, fake health, or invented operational data
- expose storage-layer vocabulary as user-facing state
