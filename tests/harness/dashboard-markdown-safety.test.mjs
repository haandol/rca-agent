import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

import { REPOSITORY_ROOT } from './evaluator.mjs';

// Everything the dashboard renders as Markdown was written by a model or read
// from an S3 artifact an engine wrote, so it is untrusted input on a page whose
// own cancel and delete APIs need no authentication. These tests execute the
// renderer rather than grep its source: what matters is that no executable
// markup survives, not how the renderer is written.
const { renderMarkdown, renderMarkdownDocument } = await import(
  pathToFileURL(
    path.join(REPOSITORY_ROOT, 'packages/dashboard/app/utils/markdown.ts'),
  ).href
);

const EXECUTABLE_MARKUP = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  'text <b onclick="steal()">bold</b> more',
  '<iframe src="https://evil.example"></iframe>',
  '<svg><animate onbegin=alert(1) attributeName=x /></svg>',
  '<a href="#" onmouseover="alert(1)">hover</a>',
  '<style>body{display:none}</style>',
];

/** The tags a browser would actually parse out of the rendered HTML. */
function liveTags(html) {
  return html.match(/<[^>]*>/g) ?? [];
}

test('author HTML is shown as text instead of becoming markup', () => {
  for (const source of EXECUTABLE_MARKUP) {
    for (const render of [renderMarkdown, renderMarkdownDocument]) {
      const html = render(source);
      const tags = liveTags(html).join(' ');

      // The opening angle bracket of the author's tag must not survive, which is
      // what stops the browser from ever parsing it as an element. An escaped
      // `onerror=` left inside the visible text is inert, so only the tags the
      // browser would really see are checked.
      assert.doesNotMatch(
        tags,
        /<(script|img|iframe|svg|style|animate|b)\b/i,
        `${render.name} emitted a live tag for ${source}`,
      );
      assert.doesNotMatch(
        tags,
        /\son[a-z]+\s*=/i,
        `${render.name} emitted an event handler for ${source}`,
      );
      // The words are still readable — refusing markup must not silently blank
      // the content a reader came for.
      assert.match(html, /&lt;/, `${render.name} dropped ${source} entirely`);
    }
  }
});

test('a link or image keeps only schemes that cannot execute', () => {
  const refused = [
    '[go](javascript:alert(1))',
    '[go](JaVaScRiPt:alert(1))',
    '[go](vbscript:msgbox(1))',
    '![i](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)',
    // Browsers strip control characters before resolving a URL, so this one
    // navigates as `javascript:` even though the raw text does not read as it.
    '[go](java\tscript:alert(1))',
    '[go](java\nscript:alert(1))',
  ];

  for (const source of refused) {
    for (const render of [renderMarkdown, renderMarkdownDocument]) {
      const tags = liveTags(render(source)).join(' ');
      assert.doesNotMatch(
        tags,
        /(href|src)\s*=/i,
        `${render.name} kept a URL attribute for ${source}`,
      );
      assert.doesNotMatch(
        tags,
        /javascript:|vbscript:|data:/i,
        `${render.name} kept an executable scheme for ${source}`,
      );
    }
  }
});

test('ordinary links and images still render', () => {
  const html = renderMarkdown(
    '[docs](https://example.com/a) and ![shot](https://example.com/b.png)',
  );
  assert.match(html, /<a href="https:\/\/example\.com\/a"/);
  assert.match(html, /<img src="https:\/\/example\.com\/b\.png"/);
  // An outbound link from an untrusted document should not hand the opener over.
  assert.match(html, /rel="nofollow noopener noreferrer"/);

  // Relative and fragment targets carry no scheme to abuse and stay usable.
  assert.match(renderMarkdown('[rel](/reports/1)'), /href="\/reports\/1"/);
  assert.match(renderMarkdown('[frag](#cause)'), /href="#cause"/);
});

test('the Markdown a report actually contains still renders as Markdown', () => {
  const html = renderMarkdownDocument(
    [
      '# 근본 원인',
      '',
      '- 증거 A',
      '- 증거 B',
      '',
      '1. 첫 절차',
      '2. 둘째 절차',
      '',
      '```sql',
      'SELECT 1;',
      '```',
      '',
      '| 지표 | 값 |',
      '|---|---|',
      '| CPU | 92% |',
    ].join('\n'),
  );

  assert.match(html, /<h1>근본 원인<\/h1>/);
  assert.match(html, /<ul>[\s\S]*증거 A/);
  assert.match(html, /<ol>[\s\S]*첫 절차/);
  assert.match(html, /<code class="language-sql">/);
  assert.match(html, /<table>[\s\S]*CPU/);
});

test('model output with escaped newlines and run-together list items reads as a list', () => {
  // Field-level model output arrives this way often enough that dropping the
  // repair would render a numbered procedure as one paragraph.
  const html = renderMarkdown('1. 첫 단계 2. 둘째 단계');
  assert.match(html, /<ol>/);
  assert.match(renderMarkdown('앞줄\\n뒷줄'), /<br>/);
});

test('every page that renders untrusted Markdown goes through this renderer', async () => {
  const { readFile } = await import('node:fs/promises');
  const pages = [
    'packages/dashboard/app/pages/report/[id].vue',
    'packages/dashboard/app/pages/playbook/[id].vue',
    'packages/dashboard/app/pages/trace/[id].vue',
  ];

  for (const page of pages) {
    const source = await readFile(path.join(REPOSITORY_ROOT, page), 'utf8');
    // Importing `marked` directly is how the raw-HTML passthrough got in, so a
    // page reaching past this module is the regression to catch.
    assert.doesNotMatch(
      source,
      /from 'marked'/,
      `${page} imports marked directly instead of the safe renderer`,
    );
    assert.match(
      source,
      /from '~\/utils\/markdown'/,
      `${page} renders Markdown without the safe renderer`,
    );
  }
});
