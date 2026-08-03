/**
 * The causal chain a finished report states, pulled out of its Markdown.
 *
 * The search is a parallel one — several hypotheses are pursued at once and most
 * are rejected — but the *finding* is linear: one cause led to the next, down to
 * the thing that has to change. The report already writes that chain as its 5
 * Whys, and it is the only linear account either engine produces.
 *
 * Both engines write the section under a `## 5 Whys` heading, and neither writes
 * it as structured data, so this reads the prose. That makes the parse the weak
 * point rather than the display: every function here returns empty instead of
 * throwing, and a report whose section drifts simply shows no chain rather than
 * showing a wrong one or breaking the page.
 */

export interface CausalLink {
  /** Position in the chain, from the symptom down. */
  index: number;
  /** What was asked at this depth. */
  question: string;
  /** What the evidence answered. */
  answer: string;
}

/** The heading both engines use, in either language. */
const SECTION_HEADING = /^#{1,3}\s*5\s*whys\b/i;

/**
 * The arrow separating a question from its answer.
 *
 * Strands writes '→', CC Headless writes '—', and an em dash also shows up mid
 * sentence — so the split takes the first occurrence only, which is the one that
 * ends the question.
 */
const SEPARATORS = ['→', '—', '⇒', '->'];

function splitOnFirstSeparator(line: string): [string, string] | null {
  for (const separator of SEPARATORS) {
    const at = line.indexOf(separator);
    if (at > 0) {
      return [line.slice(0, at), line.slice(at + separator.length)];
    }
  }
  return null;
}

/** Strips list bullets and the leading ordinal both engines write. */
function stripMarkers(line: string): string {
  return line
    .replace(/^\s*[-*•]\s*/, '')
    .replace(/^\s*\d+[.)]\s*/, '')
    .trim();
}

/**
 * Removes inline Markdown from a string that will be shown as plain text.
 *
 * Model-written fields are shown as prose rather than run through the Markdown
 * renderer, so an emphasis marker the model wrote arrives on screen as literal
 * asterisks ('**확정 여부**: 확정'). Rendering them as HTML instead would mean
 * putting untrusted model output through a second path; stripping the few inline
 * markers keeps it on the one path that cannot produce markup at all.
 *
 * Also drops a leading bullet or ordinal, since a single field lifted out of a
 * list carries the marker that made sense only inside it.
 */
export function stripInlineMarkup(text: string | undefined | null): string {
  if (!text) return '';
  return (
    text
      .replace(/^\s*[-*•]\s+/, '')
      .replace(/\*\*(.+?)\*\*/g, '$1')
      .replace(/(^|[\s(])\*(?!\s)(.+?)(?<!\s)\*(?=[\s).,;:]|$)/g, '$1$2')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      // Dangling markers left by a field sliced out of a longer sentence, e.g.
      // '확정(CONFIRMED)** — 신뢰도 0.93' where the opening pair was cut away.
      .replace(/\*{1,2}/g, '')
      .replace(/\s{2,}/g, ' ')
      .trim()
  );
}

/**
 * The lines of the 5 Whys section, in order.
 *
 * Reads until the next heading of the same or higher level, so a subsection
 * inside 5 Whys would be included while the following section is not.
 */
function sectionLines(markdown: string): string[] {
  const lines = markdown.split('\n');
  const start = lines.findIndex((line) => SECTION_HEADING.test(line.trim()));
  if (start < 0) return [];

  const collected: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (/^#{1,3}\s/.test(line.trim())) break;
    if (line.trim()) collected.push(line);
  }
  return collected;
}

export function parseCausalChain(
  markdown: string | undefined | null,
): CausalLink[] {
  if (!markdown) return [];

  const links: CausalLink[] = [];
  for (const raw of sectionLines(markdown)) {
    const line = stripMarkers(raw);
    if (!line) continue;

    const split = splitOnFirstSeparator(line);
    // A line with no separator is prose around the chain rather than a link in
    // it, so it is skipped instead of being shown as a question with no answer.
    if (!split) continue;

    const [question, answer] = split;
    const trimmedQuestion = stripInlineMarkup(question).replace(
      /\s*[?？]\s*$/,
      '',
    );
    const trimmedAnswer = stripInlineMarkup(answer);
    if (!trimmedQuestion || !trimmedAnswer) continue;

    links.push({
      index: links.length + 1,
      question: trimmedQuestion,
      answer: trimmedAnswer,
    });
  }

  return links;
}

/**
 * The moments a report puts on a clock, pulled out of its Timeline section.
 *
 * A timestamp is what makes two facts comparable — the deploy at 05:40:02 and
 * the connection spike at 05:40:00 are the whole finding — so the times are
 * parsed out and shown as a spine rather than left inside a bullet list.
 */
export interface TimelineMoment {
  /** The clock reading as the report wrote it, e.g. '05:40:02'. */
  time: string;
  /** What happened, with the time and its punctuation removed. */
  event: string;
}

const TIMELINE_HEADING = /^#{1,3}\s*(timeline|타임라인|시간\s*순|경과)\b/i;

/** A clock reading at the start of a line: 05:40, 05:40:02, or 05:41~05:42. */
const LEADING_TIME =
  /^(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[~-]\s*\d{1,2}:\d{2}(?::\d{2})?)?)/;

export function parseTimeline(
  markdown: string | undefined | null,
): TimelineMoment[] {
  if (!markdown) return [];

  const lines = markdown.split('\n');
  const start = lines.findIndex((line) => TIMELINE_HEADING.test(line.trim()));
  if (start < 0) return [];

  const moments: TimelineMoment[] = [];
  for (const raw of lines.slice(start + 1)) {
    if (/^#{1,3}\s/.test(raw.trim())) break;
    const line = stripMarkers(raw);
    if (!line) continue;

    const match = LEADING_TIME.exec(line);
    // Lines without a leading clock reading describe the investigation rather
    // than a moment in the incident, so they are left out of the spine.
    if (!match?.[1]) continue;

    const event = stripInlineMarkup(
      line.slice(match[1].length).replace(/^\s*(UTC|KST)?\s*[:·—-]?\s*/i, ''),
    );
    if (!event) continue;

    moments.push({ time: match[1].replace(/\s+/g, ''), event });
  }

  return moments;
}
