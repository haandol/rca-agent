import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import test from 'node:test';

const root = path.resolve(import.meta.dirname, '../..');
const dashboardRequire = createRequire(
  path.join(root, 'packages/dashboard/package.json'),
);
const nuxtRequire = createRequire(
  dashboardRequire.resolve('nuxt/package.json'),
);
const vue = nuxtRequire('vue');
const { parse, compileScript } = nuxtRequire('vue/compiler-sfc');
const { renderToString } = nuxtRequire('vue/server-renderer');
const ts = dashboardRequire('typescript');
const appRoot = path.join(root, 'packages/dashboard/app');
function transpile(source) {
  return ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText;
}
function utility(filename) {
  const module = { exports: {} };
  new Function(
    'require',
    'module',
    'exports',
    transpile(readFileSync(filename, 'utf8')),
  )(dashboardRequire, module, module.exports);
  return module.exports;
}
function compile(relative, globals, expose = '') {
  const filename = path.join(appRoot, relative);
  let source = readFileSync(filename, 'utf8');
  if (expose)
    source = source.replace(
      '</script>',
      `defineExpose({${expose}});\n</script>`,
    );
  const { descriptor } = parse(source, { filename });
  const compiled = compileScript(descriptor, {
    id: relative,
    inlineTemplate: true,
    templateOptions: { ssr: true },
  });
  const module = { exports: {} };
  const require = (id) =>
    id.startsWith('~/utils/')
      ? utility(path.join(appRoot, id.replace('~/', '') + '.ts'))
      : nuxtRequire(id);
  new Function(
    'require',
    'module',
    'exports',
    ...Object.keys(globals),
    transpile(compiled.content),
  )(require, module, module.exports, ...Object.values(globals));
  return module.exports.default;
}
async function report({
  playbookFailure = false,
  reportFailure = false,
  operation,
  postError,
  dataOverrides = {},
  routeEngine = 'headless-codex',
} = {}) {
  const calls = [];
  const refreshes = [];
  const book = {
    playbook_id: 'current',
    playbookDigest: 'a'.repeat(64),
    executable: true,
    verification_status: 'DRAFT',
    execution_steps: [
      {
        step_id: 's1',
        intent: '관측 소유자 중지',
        action: '중지',
        success_criteria: '중지 확인',
        commands: [
          'aws ecs stop-task --cluster current-cluster --task current-task --region us-east-1',
        ],
      },
    ],
  };
  const data = {
    '/api/reports/fixture': {
      rcaId: 'fixture',
      engine: 'headless-codex',
      markdown: '# 원문\n\nSECRET RAW MARKER',
      displayMarkdown: '# 보고서 본문\n\n원본 근거 보존',
      summary: {
        impactSummary: 'API 요청 지연',
        severity: 'high',
        confidence: 0.92,
        nextAction: '소유자 확인',
      },
    },
    '/api/sessions/fixture': {
      rcaId: 'fixture',
      alarmName: '서비스 장애',
      engine: 'headless-codex',
      state: 'COMPLETED',
      confirmed: true,
      rootCause: '확정 원인',
      readiness: 'AWAITING_APPROVAL',
    },
    '/api/playbooks/fixture': book,
    '/api/executions/fixture': {
      executions: [
        {
          executionId: 'previous',
          state: 'FAILED',
          stateLabel: '실패',
          attempt: 1,
          attemptedStepCount: 1,
          blockedCount: 0,
          failedStepCount: 1,
        },
      ],
    },
    '/api/playbook-proposals/fixture': {
      rca_id: 'fixture',
      engine: 'headless-codex',
      comparison: null,
      unavailable_reason: null,
    },
  };
  Object.assign(data, dataOverrides);
  const entries = new Map();
  const globals = {
    ...vue,
    useRoute: () => ({
      params: { id: 'fixture' },
      query: { engine: routeEngine },
    }),
    useHead: () => {},
    useFetch: (url) => {
      const actual = typeof url === 'function' ? url() : url;
      const failure =
        (playbookFailure && actual === '/api/playbooks/fixture') ||
        (reportFailure && actual === '/api/reports/fixture');
      const entry = {
        data: vue.ref(failure ? null : data[actual]),
        error: vue.ref(failure ? new Error('fixture failure') : null),
        status: vue.ref(failure ? 'error' : 'success'),
        refresh: async () => {
          refreshes.push(actual);
        },
      };
      entries.set(actual, entry);
      return entry;
    },
    $fetch: async (url, options) => {
      calls.push({ url, options });
      if (typeof postError === 'function') await postError();
      else if (postError) throw postError;
      return { markdown: 'evidence' };
    },
  };
  const exposedNames =
    'openDetail,activeDetail,detailOpen,visited,reviewed,canApprove,approveExecution,reviewedDigest,pendingApprovalId,reloadPlanForReview,showEvidence,evidenceError,evidence,approvalError,reviewRecovery,pollAnalysisParts,pendingRecoveryRequest,retransmitRecovery,resolvedEngine,analysisHandoff';
  const page = compile('pages/report/[id].vue', globals, exposedNames);
  const originalSetup = page.setup;
  let controls;
  page.setup = async (props, context) => {
    const renderer = originalSetup(props, {
      ...context,
      expose: (value) => {
        controls = value;
      },
    });
    if (operation) await operation(controls, entries, calls);
    return renderer;
  };
  const app = vue.createSSRApp(page);
  const warnings = [];
  app.config.warnHandler = (message) => warnings.push(message);
  for (const name of [
    'DetailDialog',
    'ReadState',
    'PlaybookKnowledge',
    'PlaybookComparison',
    'RecoveryPlanSteps',
    'MetricWaitDetails',
    'AnalysisParts',
    'CausalChain',
  ])
    app.component(name, compile(`components/${name}.vue`, globals));
  app.component('NuxtLink', {
    props: ['to'],
    template: '<a :href="to"><slot /></a>',
  });
  // These components own separate APIs; the report's switching is tested here.
  // ReadState's successful slot renders a fragment, so preserve that boundary.
  app.component('AnalysisDetails', {
    template:
      '<section>분석 패널 고유 내용</section><section>분석 가설 목록</section>',
  });
  app.component('RetrospectiveDetails', {
    template: '<section>실행 증거와 회고</section>',
  });
  const html = await renderToString(app);
  return { html, controls, calls, entries, refreshes, warnings };
}

test('visiting analysis then comparison hides all analysis content despite its fragment root', async () => {
  const { html, controls, warnings } = await report({
    operation: (state) => {
      state.openDetail('analysis');
      state.openDetail('comparison');
    },
  });
  assert.equal(
    controls.visited.analysis,
    true,
    'analysis remains mounted to preserve selection',
  );
  assert.equal(controls.activeDetail.value, 'comparison');
  const panel = html.match(
    /<div([^>]*data-detail-panel="analysis"[^>]*)>([\s\S]*?)<\/div>/,
  );
  assert.ok(panel, 'analysis visibility has a native element boundary');
  assert.match(panel[1], /style="[^"]*display:\s*none/);
  assert.match(panel[2], /분석 패널 고유 내용/);
  assert.match(panel[2], /분석 가설 목록/);
  assert.ok(
    !warnings.some((message) => /directive|non-element root/.test(message)),
    warnings.join('\n'),
  );
  assert.equal((html.match(/<dialog\b/g) || []).length, 1);
});

test('report first view retains authoritative summary and uses displayMarkdown for the full original', async () => {
  const { html } = await report();
  assert.match(html, /확정 원인/);
  assert.match(html, /API 요청 지연/);
  assert.match(html, /92%/);
  assert.match(html, /원본 근거 보존/);
  assert.doesNotMatch(html, /SECRET RAW MARKER/);
  assert.equal((html.match(/<dialog\b/g) || []).length, 1);
  assert.doesNotMatch(html, /href="\/(trace|playbook|retrospective)\//);
});

test('runbook detail contains full commands, explicit review and approval in the same single dialog', async () => {
  const result = await report({
    operation: (state) => state.openDetail('runbook'),
  });
  assert.match(
    result.html,
    /aws ecs stop-task --cluster current-cluster --task current-task --region us-east-1/,
  );
  assert.match(result.html, /중지 확인/);
  assert.match(result.html, /전체 명령·대상·리전·순서·성공 기준/);
  assert.equal((result.html.match(/<dialog\b/g) || []).length, 1);
  assert.equal(result.controls.reviewed.value, false);
  assert.equal(result.calls.length, 0, 'opening details is never approval');
});

test('switching details changes the same modal and approval binds the reviewed digest', async () => {
  const result = await report({
    operation: async (state) => {
      state.openDetail('runbook');
      state.openDetail('comparison');
      assert.equal(state.activeDetail.value, 'comparison');
      state.openDetail('runbook');
      state.reviewed.value = true;
      await state.approveExecution();
    },
  });
  assert.equal(result.calls.length, 1);
  assert.equal(result.calls[0].url, '/api/executions');
  assert.equal(
    result.calls[0].options.body.expectedPlaybookDigest,
    'a'.repeat(64),
  );
  assert.match(result.calls[0].options.body.approvalId, /^[0-9a-f-]{36}$/);
  assert.equal(result.controls.detailOpen.value, true);
  assert.equal(result.controls.reviewed.value, false);
  assert.deepEqual(result.refreshes.sort(), [
    '/api/executions/fixture',
    '/api/sessions/fixture',
  ]);
});

test('changed reviewed digest blocks submission and explicit reload enables a new review', async () => {
  const result = await report({
    operation: async (state, entries) => {
      state.openDetail('runbook');
      state.reviewed.value = true;
      entries.get('/api/playbooks/fixture').data.value.playbookDigest =
        'b'.repeat(64);
      await state.approveExecution();
      assert.match(state.approvalError.value, /변경/);
      await state.reloadPlanForReview();
      assert.equal(state.reviewed.value, false);
      assert.equal(state.reviewedDigest.value, 'b'.repeat(64));
    },
  });
  assert.equal(result.calls.length, 0);
});

test('playbook failure leaves incident summary and execution evidence independently available', async () => {
  const result = await report({
    playbookFailure: true,
    operation: (state) => state.openDetail('executions'),
  });
  assert.match(result.html, /확정 원인/);
  assert.match(result.html, /실패/);
  assert.match(result.html, /실행 증거 · 회고/);
  assert.equal(result.controls.canApprove.value, false);
});

test('report failure retains the summary and offers a retry within details', async () => {
  const { html, controls } = await report({ reportFailure: true });
  assert.match(html, /확정 원인/);
  assert.match(html, /다시 불러오기/);
  assert.equal(controls.canApprove.value, false);
});

test('evidence failure remains in the same modal with retry instead of replacing the incident', async () => {
  const { html, controls } = await report({
    postError: new Error('evidence offline'),
    operation: (state) => state.showEvidence('h1'),
  });
  assert.equal(controls.activeDetail.value, 'evidence');
  assert.equal(controls.evidenceError.value, true);
  assert.match(html, /확정 원인/);
  assert.match(html, /전체 증거 불러오지 못했습니다/);
  assert.equal((html.match(/<dialog\b/g) || []).length, 1);
});

test('library SSR initializes its cursor after the asynchronous first page, matching hydrated payload', async () => {
  const firstPage = { items: [], nextCursor: 'legacy-next-page' };
  const globals = {
    ...vue,
    useHead: () => {},
    useFetch: () => {
      const state = {
        data: vue.ref(null),
        status: vue.ref('pending'),
        error: vue.ref(null),
        refresh: async () => {},
      };
      const pending = Promise.resolve().then(() => {
        state.data.value = firstPage;
        state.status.value = 'success';
        return state;
      });
      vue.onServerPrefetch(() => pending);
      return Object.assign(pending, state);
    },
  };
  const page = compile('pages/playbooks.vue', globals);
  const app = vue.createSSRApp(page);
  for (const name of ['DetailDialog', 'ReadState', 'PlaybookKnowledge'])
    app.component(name, compile(`components/${name}.vue`, globals));
  const html = await renderToString(app);
  assert.match(html, /<button[^>]*>\s*다음 페이지\s*<\/button>/);
  assert.equal((html.match(/<dialog\b/g) || []).length, 1);
});

test('NO_MATCH shows the actual frozen current-report input rather than a similar-candidate list', async () => {
  const result = await report({
    operation: (state, entries) => {
      entries.get('/api/playbook-proposals/fixture').data.value.comparison = {
        status: 'NO_MATCH',
        query: 'query',
        selected_playbook_id: null,
        candidates: [{ playbook_id: 'CONSIDERED_NOT_USED', similarity: 0.9 }],
        used_references: [
          { role: 'current-runbook-input', ref: 'current_report' },
        ],
        inputs: { current_report: 'ACTUAL_FROZEN_REPORT_INPUT' },
        evidence: [],
      };
      state.openDetail('comparison');
    },
  });
  assert.match(result.html, /ACTUAL_FROZEN_REPORT_INPUT/);
  assert.match(result.html, /현재 사고의 런북 생성 입력/);
  assert.doesNotMatch(result.html, /CONSIDERED_NOT_USED/);
  assert.equal(result.calls.length, 0);
});

test('NO_CHANGE displays reused baseline identity and every observation matching a used citation', async () => {
  const result = await report({
    operation: (state, entries) => {
      entries.get('/api/playbook-proposals/fixture').data.value.comparison = {
        status: 'NO_CHANGE',
        query: 'query',
        selected_playbook_id: 'historical',
        candidates: [],
        baseline: {
          playbook_id: 'historical',
          revision: 'analysis:original-revision',
          source_rca_id: 'original-source-rca',
          source_engine: 'strands',
          playbook: {
            playbook_id: 'historical',
            failure_type: 'FROZEN_KNOWLEDGE',
            execution_steps: [],
          },
        },
        used_references: [
          { role: 'knowledge-reuse', ref: 'historical' },
          { role: 'comparison-evidence', ref: 'metrics-ref' },
        ],
        evidence: [
          { ref: 'metrics-ref', value: 'FIRST_OBSERVATION' },
          { ref: 'metrics-ref', value: 'SECOND_OBSERVATION' },
          { ref: 'not-cited', value: 'NOT_A_USED_REFERENCE' },
        ],
      };
      state.openDetail('comparison');
    },
  });
  for (const text of [
    'analysis:original-revision',
    'original-source-rca',
    'FROZEN_KNOWLEDGE',
    'FIRST_OBSERVATION',
    'SECOND_OBSERVATION',
  ])
    assert.ok(result.html.includes(text), text);
  assert.doesNotMatch(result.html, /NOT_A_USED_REFERENCE/);
  assert.equal(
    result.calls.length,
    0,
    'reference provenance never reads a latest candidate',
  );
});

test('Headless references resolve the analysis input and legacy baselines are not claimed as actual use', async () => {
  const current = await report({
    operation: (state, entries) => {
      entries.get('/api/playbook-proposals/fixture').data.value.comparison = {
        status: 'NO_MATCH',
        query: '',
        selected_playbook_id: null,
        candidates: [],
        used_references: [{ role: 'current-runbook-input', ref: 'analysis' }],
        inputs: { analysis: { target: 'FROZEN_CURRENT_TARGET' } },
      };
      state.openDetail('comparison');
    },
  });
  assert.match(current.html, /FROZEN_CURRENT_TARGET/);
  const legacy = await report({
    operation: (state, entries) => {
      entries.get('/api/playbook-proposals/fixture').data.value.comparison = {
        status: 'NO_CHANGE',
        query: '',
        selected_playbook_id: 'p',
        candidates: [],
        baseline: {
          playbook_id: 'p',
          revision: 'old',
          source_rca_id: 'old-rca',
          source_engine: 'strands',
          playbook: { failure_type: 'LEGACY_BASELINE' },
        },
      };
      state.openDetail('comparison');
    },
  });
  assert.match(legacy.html, /실제 사용 여부 미기록/);
  assert.match(legacy.html, /LEGACY_BASELINE/);
});

test('summary shows compact recorded 5 Whys with the full questions available in the cause modal', async () => {
  const result = await report({
    operation: (state, entries) => {
      entries.get('/api/reports/fixture').data.value.markdown =
        '## 5 Whys\n1. 왜 지연됐나 → 연결이 고갈됐다\n2. 왜 연결이 고갈됐나 → 소유자가 반환하지 않았다';
      state.openDetail('cause');
    },
  });
  assert.match(result.html, /data-testid="summary-five-whys"/);
  assert.match(result.html, /line-clamp-2/);
  assert.match(result.html, /소유자가 반환하지 않았다/);
  assert.match(result.html, /왜 연결이 고갈됐나/);
  assert.equal((result.html.match(/<dialog\b/g) || []).length, 1);
});

function earlyPartViews() {
  return {
    '/api/sessions/fixture': {
      rcaId: 'fixture',
      engine: 'headless-codex',
      state: 'HYPOTHESIS_GENERATION',
      workflow: 'recovery-first-v1',
      confirmed: false,
      alarmName: '진행 사고',
    },
    '/api/reports/fixture': null,
    '/api/playbooks/fixture': {
      source_mode: 'recovery',
      recovery_revision: 'b'.repeat(64),
      playbookDigest: 'a'.repeat(64),
      executable: true,
      execution_steps: [
        {
          step_id: 'rollback',
          action: '정상 버전 복원',
          commands: [
            'aws ecs update-service --cluster exact --service exact --task-definition normal --region us-east-1',
          ],
          success_criteria: '정상화 관측',
        },
      ],
    },
    '/api/executions/fixture': { executions: [] },
    '/api/analysis-parts/fixture': {
      workflow: 'recovery-first-v1',
      parentEligible: true,
      parts: [
        {
          part: 'recovery',
          status: 'COMPLETED',
          available: true,
          revision: 'b'.repeat(64),
          approval_status: 'READY',
          summary: '정상화 제안',
          payload: { result: { title: '검증된 롤백' }, limitations: [] },
        },
        {
          part: 'root_cause',
          status: 'FAILED',
          available: false,
          error: '원인 원본 조회 실패',
        },
        { part: 'operations', status: 'RUNNING', available: false },
      ],
    },
  };
}
test('three-part actual page allows early review despite incomplete report and independently failed root part', async () => {
  const result = await report({
    dataOverrides: earlyPartViews(),
    operation: async (state) => {
      assert.equal(state.canApprove.value, true);
      await state.reviewRecovery();
      state.reviewed.value = true;
      await state.approveExecution();
    },
  });
  assert.equal(result.calls.length, 1);
  assert.equal(
    result.calls[0].options.body.expectedRecoveryRevision,
    'b'.repeat(64),
  );
  assert.equal(
    result.calls[0].options.body.expectedPlaybookDigest,
    'a'.repeat(64),
  );
  assert.match(result.html, /1\. 빠른 정상화/);
  assert.match(result.html, /원인 원본 조회 실패/);
  assert.match(result.html, /3\. 운영 개선/);
  assert.doesNotMatch(result.html, /SECRET RAW MARKER/);
});
test('parts continue refreshing after RESOLVED while later analysis runs, without replacing reviewed plan', async () => {
  const data = earlyPartViews();
  data['/api/executions/fixture'] = {
    executions: [
      {
        executionId: 'done',
        state: 'RESOLVED',
        stateLabel: '해결',
        attempt: 1,
      },
    ],
  };
  const savedDocument = globalThis.document;
  globalThis.document = { visibilityState: 'visible' };
  try {
    const result = await report({
      dataOverrides: data,
      operation: async (state, entries) => {
        await state.reviewRecovery();
        state.reviewed.value = true;
        entries.get(
          '/api/analysis-parts/fixture',
        ).data.value.parts[0].revision = 'c'.repeat(64);
        assert.equal(
          state.canApprove.value,
          false,
          'new revision requires explicit review',
        );
        const original = entries.get('/api/playbooks/fixture').data.value;
        await state.pollAnalysisParts();
        assert.equal(
          entries.get('/api/playbooks/fixture').data.value,
          original,
        );
      },
    });
    assert.ok(result.refreshes.includes('/api/analysis-parts/fixture'));
    assert.equal(
      result.refreshes.filter((p) => p === '/api/playbooks/fixture').length,
      1,
      'only explicit review reloads the plan',
    );
    assert.match(result.html, /실행 상태: 해결/);
    assert.match(result.html, /HYPOTHESIS_GENERATION/);
  } finally {
    globalThis.document = savedDocument;
  }
});
test('actual UI retransmits the captured UUID and revision after later analysis revokes recovery', async () => {
  let calls = 0;
  const result = await report({
    dataOverrides: earlyPartViews(),
    postError: () => {
      if (calls++ === 0)
        throw { statusCode: 503, data: { statusMessage: 'queue unavailable' } };
    },
    operation: async (state, entries) => {
      await state.reviewRecovery();
      state.reviewed.value = true;
      await state.approveExecution();
      assert.ok(state.pendingRecoveryRequest.value);
      entries.get(
        '/api/analysis-parts/fixture',
      ).data.value.parts[0].approval_status = 'REVOKED';
      entries.get('/api/sessions/fixture').data.value.state = 'CANCELLED';
      assert.equal(state.canApprove.value, false);
      await state.retransmitRecovery();
    },
  });
  assert.equal(result.calls.length, 2);
  assert.deepEqual(result.calls[0].options.body, result.calls[1].options.body);
});

test('three-part source and PR previews escape HTML and preserve explicit untested status', async () => {
  const data = earlyPartViews();
  data['/api/analysis-parts/fixture'].parts[1] = {
    part: 'root_cause',
    status: 'COMPLETED',
    available: true,
    payload: {
      result: {
        root_cause: { description: 'observed', confirmed: true },
        report_markdown: '<script>unsafe()</script>',
        code_proposal: {
          status: 'PROPOSED',
          title: 'proposed correction',
          tests_status: 'NOT_RUN',
          files: [
            {
              path: 'write.py',
              start_line: 5,
              end_line: 5,
              unified_diff: '<img src=x onerror=unsafe()>',
            },
          ],
        },
      },
      limitations: [],
    },
  };
  const result = await report({ dataOverrides: data });
  assert.doesNotMatch(result.html, /<script>|<img src=x/);
  assert.match(result.html, /NOT_RUN/);
  assert.match(result.html, /제안이며 실제 게시/);
  assert.match(result.html, /&lt;img/);
});

// These invoke the page's actual polling/controller code; no stored state is rewritten.
test('completed analysis stops part polling only when all three parts have actual terminal records', async () => {
  const data = earlyPartViews();
  data['/api/sessions/fixture'].state = 'COMPLETED';
  data['/api/analysis-parts/fixture'].parts[2].status = 'SKIPPED';
  const savedDocument = globalThis.document;
  globalThis.document = { visibilityState: 'visible' };
  try {
    const result = await report({
      dataOverrides: data,
      operation: async (state) => {
        await state.pollAnalysisParts();
        await state.pollAnalysisParts();
      },
    });
    assert.deepEqual(result.refreshes, []);
    const missing = earlyPartViews();
    missing['/api/sessions/fixture'].state = 'COMPLETED';
    missing['/api/analysis-parts/fixture'].parts = [];
    const pending = await report({
      dataOverrides: missing,
      operation: async (state) => {
        await state.pollAnalysisParts();
      },
    });
    assert.ok(
      pending.refreshes.includes('/api/analysis-parts/fixture'),
      'an empty response cannot stand in for three terminal parts',
    );
  } finally {
    globalThis.document = savedDocument;
  }
});
for (const parentState of ['CANCELLED', 'FAILED', 'OUTDATED']) {
  test(`parent ${parentState} labels fenced parts honestly and preserves completed recovery and RESOLVED execution`, async () => {
    const data = earlyPartViews();
    data['/api/sessions/fixture'].state = parentState;
    const parts = data['/api/analysis-parts/fixture'].parts;
    parts[1] = {
      part: 'root_cause',
      status: 'RUNNING',
      available: false,
      summary: '마지막 관측 유지',
    };
    parts[2] = { part: 'operations', status: 'WAITING', available: false };
    data['/api/executions/fixture'] = {
      executions: [
        {
          executionId: 'resolved',
          state: 'RESOLVED',
          stateLabel: '해결',
          attempt: 1,
        },
      ],
    };
    const before = structuredClone(parts);
    const savedDocument = globalThis.document;
    globalThis.document = { visibilityState: 'visible' };
    try {
      const result = await report({
        dataOverrides: data,
        operation: async (state) => {
          await state.pollAnalysisParts();
          await state.pollAnalysisParts();
        },
      });
      assert.equal(
        result.refreshes.filter((url) => url === '/api/analysis-parts/fixture')
          .length,
        1,
        'one final read after parent termination, not endless polling',
      );
      assert.deepEqual(
        result.entries.get('/api/analysis-parts/fixture').data.value.parts,
        before,
      );
      assert.match(result.html, /작업 중단/);
      assert.match(result.html, /마지막 저장 상태는 RUNNING/);
      assert.match(result.html, /시작 기록 없음/);
      assert.match(result.html, /마지막 저장 상태는 WAITING/);
      assert.match(result.html, /실행 상태: 해결/);
      assert.match(result.html, /검증된 롤백/);
      assert.doesNotMatch(result.html, /분석 결과를 준비하고 있습니다/);
      const recoveryHtml = result.html
        .split('data-part="recovery"')[1]
        .split('data-part="root_cause"')[0];
      assert.doesNotMatch(recoveryHtml, /작업 중단|마지막 저장 상태/);
    } finally {
      globalThis.document = savedDocument;
    }
  });
}
test('part polling retries failed terminal refresh and remains active before workflow registration', async () => {
  const savedDocument = globalThis.document;
  globalThis.document = { visibilityState: 'visible' };
  try {
    const data = earlyPartViews();
    data['/api/sessions/fixture'].state = 'FAILED';
    const result = await report({
      dataOverrides: data,
      operation: async (state, entries) => {
        const entry = entries.get('/api/analysis-parts/fixture');
        entry.error.value = new Error('read unavailable');
        await state.pollAnalysisParts();
        await state.pollAnalysisParts();
        entry.error.value = null;
        await state.pollAnalysisParts();
        await state.pollAnalysisParts();
      },
    });
    assert.equal(
      result.refreshes.filter((url) => url === '/api/analysis-parts/fixture')
        .length,
      3,
    );
    const initial = earlyPartViews();
    delete initial['/api/sessions/fixture'].workflow;
    initial['/api/analysis-parts/fixture'] = { workflow: null, parts: [] };
    const initialResult = await report({
      dataOverrides: initial,
      operation: async (state) => {
        await state.pollAnalysisParts();
      },
    });
    assert.ok(initialResult.refreshes.includes('/api/analysis-parts/fixture'));
  } finally {
    globalThis.document = savedDocument;
  }
});

test('takeover UI shows original engine RESOLVED and blocks duplicate approval for prior-engine active reservation', async () => {
  const data = earlyPartViews();
  data['/api/executions/fixture'] = {
    executionScope: 'rca',
    activeExecutionId: '',
    executions: [
      {
        executionId: 'old-strands',
        engine: 'strands',
        state: 'RESOLVED',
        stateLabel: '해결',
        attempt: 1,
      },
    ],
  };
  const result = await report({ dataOverrides: data });
  assert.match(result.html, /실행 상태: 해결/);
  assert.match(result.html, /실행 원본 엔진 strands/);
  assert.match(result.html, /HYPOTHESIS_GENERATION/);
  data['/api/executions/fixture'].activeExecutionId = 'old-strands';
  data['/api/executions/fixture'].activeExecutionEngine = 'strands';
  const active = await report({
    dataOverrides: data,
    operation: (state) => {
      assert.equal(state.canApprove.value, false);
      state.openDetail('executions');
    },
  });
  assert.match(active.html, /활성 실행 예약: old-strands/);
  assert.match(active.html, /실행 원본 엔진: strands/);
});

test('an already-open Strands view announces takeover without swapping its reviewed runbook', async () => {
  const data = earlyPartViews();
  data['/api/sessions/fixture'].engine = 'strands';
  data['/api/sessions/fixture'].activeEngine = 'strands';
  data['/api/analysis-parts/fixture'].engine = 'strands';
  data['/api/analysis-parts/fixture'].activeEngine = 'strands';
  const savedDocument = globalThis.document;
  globalThis.document = { visibilityState: 'visible' };
  try {
    const result = await report({
      routeEngine: 'strands',
      dataOverrides: data,
      operation: async (state, entries) => {
        state.openDetail('runbook');
        state.reviewed.value = true;
        const book = entries.get('/api/playbooks/fixture').data.value,
          digest = state.reviewedDigest.value;
        Object.assign(entries.get('/api/sessions/fixture').data.value, {
          engine: 'headless-codex',
          activeEngine: 'headless-codex',
          engineHandoff: true,
          state: 'COMPLETED',
        });
        const parts = entries.get('/api/analysis-parts/fixture').data.value;
        parts.activeEngine = 'headless-codex';
        parts.historicalView = true;
        parts.parentEligible = false;
        parts.parts[1] = {
          part: 'root_cause',
          status: 'RUNNING',
          available: false,
        };
        parts.parts[2] = {
          part: 'operations',
          status: 'WAITING',
          available: false,
        };
        assert.equal(state.resolvedEngine.value, 'strands');
        assert.equal(state.analysisHandoff.value, true);
        assert.equal(state.canApprove.value, false);
        assert.equal(entries.get('/api/playbooks/fixture').data.value, book);
        assert.equal(state.reviewedDigest.value, digest);
        await state.pollAnalysisParts();
        await state.approveExecution();
      },
    });
    assert.equal(result.calls.length, 0);
    assert.equal(result.refreshes.length, 0);
    assert.match(result.html, /분석 담당 엔진이 변경되었습니다/);
    assert.match(result.html, /현재 분석 담당은 headless-codex/);
    assert.match(
      result.html,
      /href="\/report\/fixture\?engine=headless-codex"/,
    );
    assert.match(result.html, /이전 엔진의 마지막 기록: RUNNING/);
    assert.match(result.html, /이전 엔진의 마지막 기록: WAITING/);
    assert.doesNotMatch(result.html, /분석 결과를 준비하고 있습니다/);
  } finally {
    globalThis.document = savedDocument;
  }
});
