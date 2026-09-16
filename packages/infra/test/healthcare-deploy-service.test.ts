import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const NEW_DIGEST = `sha256:${'ab'.repeat(32)}`;
const CURRENT_DIGEST = `sha256:${'cd'.repeat(32)}`;
let directory: string;

beforeEach(() => {
  directory = fs.mkdtempSync(
    path.join(os.tmpdir(), 'healthcare-deploy-helper-'),
  );
});
afterEach(() => fs.rmSync(directory, { recursive: true, force: true }));

/** Run the complete helper with fake AWS, Docker and CDK executables, never external services. */
function deploy(target: string, mode = 'normal', skipBuild = true) {
  const mock = `#!${process.execPath}
const fs = require('fs');
const path = require('path');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const arg = key => args.includes(key) ? args[args.indexOf(key) + 1] : undefined;
const log = entry => fs.appendFileSync(process.env.MOCK_LOG, JSON.stringify(entry) + '\\n');
log({name, args});
const mode = process.env.MOCK_MODE;
const td = 'arn:aws:ecs:us-east-1:123456789012:task-definition/' + (arg('--services') || 'RcaAgentDevHealthcare') + ':7';
if (name === 'npx') {
  log({synthEnv: Object.fromEntries(Object.entries(process.env).filter(([key]) => key.endsWith('_IMAGE_TAG') || key === 'HEALTHCARE_IMAGE_DIGEST'))});
} else if (name === 'docker') {
  if (args[0] === 'login') process.stdin.resume();
} else if (name === 'aws') {
  const op = args.slice(0, 2).join(' ');
  if (op === 'sts get-caller-identity') console.log('123456789012');
  else if (op === 'ecr get-login-password') console.log('mock-login');
  else if (op === 'ecr describe-images') console.log(mode === 'missing-ecr' ? 'None' : '${NEW_DIGEST}');
  else if (op === 'ecs describe-services') {
    if (mode === 'service-failure') process.exit(2);
    console.log(td);
  } else if (op === 'ecs describe-task-definition') {
    const requested = arg('--task-definition');
    if (!requested.startsWith('arn:') || !requested.endsWith(':7')) process.exit(3);
    const service = requested.split('/').pop().split(':')[0];
    const app = {
      RcaAgentDevRcaAgent: ['rca-agent', 'rca-agent'],
      RcaAgentDevCcHeadless: ['cc-headless', 'cc-headless'],
      RcaAgentDevPlaybookExecution: ['playbook-execution', 'cc-headless'],
      RcaAgentDevHealthcare: ['healthcare', 'healthcare'],
    }[service];
    if (!app || arg('--query')) process.exit(5);
    const repository = '123456789012.dkr.ecr.us-east-1.amazonaws.com/rcaagentdev/' + app[1];
    const container = {name: app[0], image: repository + ':current-label'};
    if (app[0] === 'healthcare') {
      container.image = mode === 'pin-disagreement' ? repository + '@${NEW_DIGEST}' : repository + ':old-label';
      container.environment = [{name: 'DEPLOYED_REVISION', value: 'observed-label'}];
    }
    let containers = [container];
    if (mode === 'sidecar-first') containers.unshift({name: 'otel-collector', image: 'public.ecr.aws/aws-observability/aws-otel-collector:sidecar-tag'});
    if (app[0] !== 'healthcare') {
      if (mode === 'ambiguous-app') containers.push({...container, name: 'other-app'});
      if (mode === 'missing-app') containers = [{name: 'otel-collector', image: 'public.ecr.aws/otel:sidecar-tag'}];
      if (mode === 'wrong-repository') container.image = repository + '-other:foreign-tag';
      if (mode === 'wrong-container') container.name = 'otel-collector';
      if (mode === 'digest-only') container.image = repository + '@${CURRENT_DIGEST}';
    }
    console.log(JSON.stringify({taskDefinition: {taskDefinitionArn: mode === 'wrong-definition' ? requested.replace(':7', ':8') : requested, containerDefinitions: containers}}));
  } else if (op === 'ecs list-tasks') console.log(mode === 'no-tasks' ? 'None' : 'arn:task/owned');
  else if (op === 'ecs describe-tasks') {
    const task = {taskDefinitionArn: mode === 'rolling' ? td.replace(':7', ':8') : td,
      lastStatus: 'RUNNING', containers: [{name: 'healthcare', imageDigest: mode === 'missing-observed' ? '' : '${CURRENT_DIGEST}'}]};
    const tasks = [task];
    if (mode === 'mixed') tasks.push({...task, containers: [{name: 'healthcare', imageDigest: '${NEW_DIGEST}'}]});
    console.log(JSON.stringify({tasks}));
  } else { console.error('Unexpected AWS operation', op); process.exit(4); }
}
`;
  for (const name of ['aws', 'docker', 'npx'])
    fs.writeFileSync(path.join(directory, name), mock, { mode: 0o755 });
  const logFile = path.join(directory, 'calls.jsonl');
  const result = spawnSync(
    'bash',
    [
      path.resolve(__dirname, '../scripts/deploy-service.sh'),
      ...(skipBuild ? ['--skip-build'] : []),
      '--tag',
      'new-label',
      target,
    ],
    {
      env: {
        ...process.env,
        PATH: `${directory}:${process.env.PATH}`,
        MOCK_LOG: logFile,
        MOCK_MODE: mode,
        HEALTHCARE_IMAGE_DIGEST: `sha256:${'ef'.repeat(32)}`,
      },
      encoding: 'utf8',
      timeout: 15000,
    },
  );
  const calls = fs
    .readFileSync(logFile, 'utf8')
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line));
  return { result, calls, env: calls.find((call) => call.synthEnv)?.synthEnv };
}

test('newly built Healthcare tag resolves its own ECR digest before CDK', () => {
  const { result, calls, env } = deploy('healthcare', 'normal', false);
  expect(result.status).toBe(0);
  expect(env).toMatchObject({
    HEALTHCARE_IMAGE_TAG: 'new-label',
    HEALTHCARE_IMAGE_DIGEST: NEW_DIGEST,
    AGENT_IMAGE_TAG: 'current-label',
    HEADLESS_CODEX_IMAGE_TAG: 'current-label',
    EXECUTION_IMAGE_TAG: 'current-label',
  });
  const push = calls.findIndex(
    (call) => call.name === 'docker' && call.args[0] === 'push',
  );
  const lookup = calls.findIndex(
    (call) => call.name === 'aws' && call.args[1] === 'describe-images',
  );
  expect(push).toBeGreaterThan(-1);
  expect(lookup).toBeGreaterThan(push);
  expect(calls[lookup].args).toContain('imageTag=new-label');
  expect(calls.some((call) => call.args?.includes('describe-tasks'))).toBe(
    false,
  );
});

test('non-target Healthcare preserves observed digest and label without resolving its old ECR tag', () => {
  const { result, calls, env } = deploy('agent');
  expect(result.status).toBe(0);
  expect(env).toMatchObject({
    AGENT_IMAGE_TAG: 'new-label',
    HEALTHCARE_IMAGE_TAG: 'observed-label',
    HEALTHCARE_IMAGE_DIGEST: CURRENT_DIGEST,
  });
  expect(calls.some((call) => call.args?.includes('describe-images'))).toBe(
    false,
  );
  const definitions = calls.filter((call) =>
    call.args?.includes('describe-task-definition'),
  );
  expect(definitions.length).toBeGreaterThan(0);
  for (const call of definitions)
    expect(call.args[call.args.indexOf('--task-definition') + 1]).toMatch(
      /^arn:.*:7$/,
    );
});

test.each([
  'no-tasks',
  'missing-observed',
  'mixed',
  'rolling',
  'pin-disagreement',
  'service-failure',
])('unproven current Healthcare baseline prevents CDK: %s', (mode) => {
  const { result, env, calls } = deploy('agent', mode);
  expect(result.status).not.toBe(0);
  expect(env).toBeUndefined();
  expect(calls.some((call) => call.name === 'npx')).toBe(false);
});

test('missing target ECR digest prevents CDK despite an inherited digest', () => {
  const { result, env } = deploy('healthcare', 'missing-ecr');
  expect(result.status).not.toBe(0);
  expect(env).toBeUndefined();
});

test.each(['healthcare', 'agent', 'execution'])(
  'sidecar-first task definitions preserve only app tags when deploying %s',
  (target) => {
    const { result, env, calls } = deploy(target, 'sidecar-first');
    expect(result.status).toBe(0);
    expect(env.AGENT_IMAGE_TAG).toBe(
      target === 'agent' ? 'new-label' : 'current-label',
    );
    expect(env.HEADLESS_CODEX_IMAGE_TAG).toBe('current-label');
    expect(env.EXECUTION_IMAGE_TAG).toBe(
      target === 'execution' ? 'new-label' : 'current-label',
    );
    expect(env.HEALTHCARE_IMAGE_TAG).toBe(
      target === 'healthcare' ? 'new-label' : 'observed-label',
    );
    expect(env.HEALTHCARE_IMAGE_DIGEST).toBe(
      target === 'healthcare' ? NEW_DIGEST : CURRENT_DIGEST,
    );
    expect(Object.values(env)).not.toContain('sidecar-tag');
    const definitions = calls.filter((call) =>
      call.args?.includes('describe-task-definition'),
    );
    for (const call of definitions) {
      expect(call.args).not.toContain('--query');
      expect(call.args[call.args.indexOf('--task-definition') + 1]).toMatch(
        /^arn:.*:7$/,
      );
    }
  },
);

test.each([
  'ambiguous-app',
  'missing-app',
  'wrong-repository',
  'wrong-container',
  'digest-only',
  'wrong-definition',
])('unproven non-target app identity prevents CDK: %s', (mode) => {
  const { result, env, calls } = deploy('healthcare', mode);
  expect(result.status).not.toBe(0);
  expect(result.stderr).toMatch(/Preserved/);
  expect(env).toBeUndefined();
  expect(calls.some((call) => call.name === 'npx')).toBe(false);
});
