import { Marked } from 'marked';

/**
 * Renders Markdown that came from a model or an S3 artifact.
 *
 * Everything this dashboard renders as Markdown is untrusted: report bodies,
 * playbook prose, hypothesis reasoning and evidence text are all written by a
 * model, and the S3 objects behind them are written by the engines rather than
 * by a person. Handing that to `marked` unchanged preserves raw HTML verbatim
 * — `<img onerror=...>`, `<script>`, `javascript:` URLs — which would then run
 * on this origin and could call the unauthenticated cancel/delete APIs.
 *
 * Rather than sanitize the HTML afterwards, no raw HTML is produced in the
 * first place: the html hook escapes its source text instead of emitting it,
 * and links and images keep only schemes that cannot execute. An allowlist over
 * generated tags would have to stay in step with whatever `marked` emits next,
 * whereas refusing to emit author HTML at all leaves nothing to keep in step.
 */
const SAFE_URL_SCHEMES = new Set(['http:', 'https:', 'mailto:']);

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Drops the characters a browser ignores inside a URL.
 *
 * Browsers strip tabs and newlines before resolving a URL, so `java\tscript:`
 * navigates while a plain prefix test would not recognise the scheme at all.
 * Removing them first means the scheme this code checks is the scheme the
 * browser will act on.
 */
function stripIgnoredUrlChars(value: string): string {
  let out = '';
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    if (code <= 0x1f || code === 0x7f) continue;
    out += char;
  }
  return out;
}

/**
 * Keeps a URL only when it cannot execute script.
 *
 * Relative and fragment URLs are kept because they resolve against this origin
 * and carry no scheme to abuse. Anything that does carry a scheme has to be one
 * of the navigable ones — `javascript:`, `data:` and `vbscript:` all execute,
 * and a scheme this code does not recognise is refused too rather than given
 * the benefit of the doubt.
 */
function safeUrl(href: string | null | undefined): string | null {
  if (!href) return null;
  const cleaned = stripIgnoredUrlChars(href.trim());
  if (!cleaned) return null;
  const colon = cleaned.indexOf(':');
  if (colon > 0 && /^[a-zA-Z][a-zA-Z0-9+.-]*$/.test(cleaned.slice(0, colon))) {
    return SAFE_URL_SCHEMES.has(cleaned.slice(0, colon + 1).toLowerCase())
      ? cleaned
      : null;
  }
  return cleaned;
}

interface LinkToken {
  href: string;
  title?: string | null;
  text: string;
}

const renderer = new Marked({ async: false, breaks: true });

renderer.use({
  renderer: {
    // Author HTML is shown as the text it is, never as markup.
    html({ text }: { text: string }) {
      return escapeHtml(text);
    },
    link({ href, title, text }: LinkToken) {
      const safe = safeUrl(href);
      const label = escapeHtml(text);
      // A refused scheme still shows its label, so the reader sees the words
      // that were written rather than a silently emptied line.
      if (!safe) return label;
      const attrs = title ? ` title="${escapeHtml(title)}"` : '';
      return `<a href="${escapeHtml(safe)}" rel="nofollow noopener noreferrer" target="_blank"${attrs}>${label}</a>`;
    },
    image({ href, title, text }: LinkToken) {
      const safe = safeUrl(href);
      const alt = escapeHtml(text ?? '');
      if (!safe) return alt;
      const attrs = title ? ` title="${escapeHtml(title)}"` : '';
      return `<img src="${escapeHtml(safe)}" alt="${alt}"${attrs}>`;
    },
  },
});

/**
 * Renders a Markdown field for display, with no executable markup surviving.
 *
 * Model output frequently arrives with literal `\n` escapes and with ordered
 * list items run together on one line, so those are normalized first — without
 * it a numbered procedure renders as a single paragraph.
 */
export function renderMarkdown(text: string | undefined | null): string {
  if (!text) return '';
  const normalized = text
    .replace(/\\n/g, '\n')
    .replace(/(?<!\n)(\d+)\.\s/g, '\n$1. ')
    .trim();
  return renderer.parse(normalized) as string;
}

/**
 * Renders a Markdown document whose source is already well-formed.
 *
 * Report bodies are whole documents rather than field fragments, so the
 * list-and-newline repair that field text needs would corrupt them.
 */
export function renderMarkdownDocument(
  text: string | undefined | null,
): string {
  if (!text) return '';
  return renderer.parse(text) as string;
}
