// Render the actual Vue components with synthetic stored data; never start Nitro/AWS.
import { createRequire } from 'node:module';
import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { deploymentBook } from './fixtures/deployment-contract.mjs';
const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(root + 'package.json');
const nuxtRequire = createRequire(require.resolve('nuxt/package.json'));
const { parse, compileScript } = nuxtRequire('vue/compiler-sfc');
const { createSSRApp, computed } = nuxtRequire('vue');
const { renderToString } = nuxtRequire('vue/server-renderer');
const { transpileModule, ModuleKind, ScriptTarget } = require('typescript');
async function component(name) {
  const filename = root + `app/components/${name}.vue`;
  const { descriptor } = parse(await readFile(filename, 'utf8'), { filename });
  const compiled = compileScript(descriptor, {
    id: name,
    inlineTemplate: true,
    templateOptions: { ssr: true },
  });
  const code = transpileModule(compiled.content, {
    compilerOptions: {
      module: ModuleKind.CommonJS,
      target: ScriptTarget.ES2022,
    },
  }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', 'computed', code)(
    nuxtRequire,
    module,
    module.exports,
    computed,
  );
  return module.exports.default;
}
const book = deploymentBook();
const app = createSSRApp(await component('RecoveryPlanSteps'), {
  steps: book.execution_steps,
  rollbackContext: book.rollback_context,
  executable: true,
});
app.component('MetricWaitDetails', await component('MetricWaitDetails'));
const html = await renderToString(app);
const assets = root + '.output/public/_nuxt/';
const styles = (await readdir(assets)).filter((f) => f.endsWith('.css'));
let css = (
  await Promise.all(styles.map((f) => readFile(assets + f, 'utf8')))
).join('\n');
// Embed the two actual design fonts so the standalone preview has no asset requests.
for (const file of (await readdir(assets)).filter((f) =>
  /^(inter-latin-wght-normal|jetbrains-mono-latin-wght-normal).*\.woff2$/.test(
    f,
  ),
)) {
  const encoded = (await readFile(assets + file)).toString('base64');
  css = css.replaceAll(file, 'data:font/woff2;base64,' + encoded);
}

await mkdir(root + 'tests/artifacts', { recursive: true });
await writeFile(
  root + 'tests/artifacts/deployment-preview.html',
  `<!doctype html><html lang="ko" data-theme="rca-ops"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" href="data:,"><title>배포 승인 계약 · 모킹 화면 검증</title><style>${css}</style><body><main class="mx-auto max-w-5xl p-6"><h1 class="text-2xl font-bold mb-4">배포 승인 계약 · 합성 데이터</h1><p class="mb-4">실제 RecoveryPlanSteps와 MetricWaitDetails 컴포넌트의 렌더링입니다. 운영 API는 호출하지 않습니다.</p>${html}</main></body></html>`,
);
console.log(root + 'tests/artifacts/deployment-preview.html');
