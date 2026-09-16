import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const TOML_DIGEST = `sha256:${'ab'.repeat(32)}`;
const ENV_DIGEST = `sha256:${'cd'.repeat(32)}`;
const CONFIG_PATH = path.resolve(__dirname, '..', '.toml');
const originalEnv = process.env;
let outdir: string;

beforeEach(() => {
  process.env = { ...originalEnv };
  for (const key of [
    'AGENT_IMAGE_TAG',
    'HEALTHCARE_IMAGE_TAG',
    'HEALTHCARE_IMAGE_DIGEST',
    'HEADLESS_CODEX_IMAGE_TAG',
    'EXECUTION_IMAGE_TAG',
    'CDK_CONTEXT_JSON',
  ]) {
    delete process.env[key];
  }
  outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'healthcare-image-config-'));
  process.env.CDK_OUTDIR = outdir;
  process.env.CDK_DEFAULT_ACCOUNT = '123456789012';
});

afterEach(() => {
  process.env = originalEnv;
  jest.dontMock('fs');
  jest.resetModules();
  fs.rmSync(outdir, { recursive: true, force: true });
});

/**
 * Feed fixture TOML to the real loader without rewriting the operator's .toml.
 * Parsing, schema validation, environment resolution and stack code stay real.
 */
function configureToml(
  digest?: unknown,
  imageTag: string | null = 'build-label',
) {
  const source = `
[app]
ns = "RcaAgent"
stage = "Dev"
[aws]
region = "us-east-1"
[alarm]
notificationEmail = "infra-test@example.com"
[agent]
imageTag = "agent-build"
[healthcare]
${imageTag === null ? '' : `imageTag = ${JSON.stringify(imageTag)}`}
${digest === undefined ? '' : `imageDigest = ${JSON.stringify(digest)}`}
[headlessCodex]
imageTag = "headless-build"
[execution]
imageTag = "execution-build"
[storage]
evidenceBucket = "infra-test-evidence"
vectorBucket = "infra-test-vectors"
[table.rcaSession]
name = "infra-test-sessions"
[tracing]
enabled = false
`;
  jest.doMock('fs', () => {
    const actual = jest.requireActual<typeof import('fs')>('fs');
    return {
      ...actual,
      readFileSync: (...args: Parameters<typeof fs.readFileSync>) => {
        const [file, options] = args;
        if (file !== CONFIG_PATH) return actual.readFileSync(...args);
        const encoding =
          typeof options === 'string' ? options : options?.encoding;
        const bytes = Buffer.from(source, 'utf8');
        return encoding ? bytes.toString(encoding) : bytes;
      },
    };
  });
}

test('non-config reads retain real filesystem content and missing-file errors', () => {
  const otherConfig = path.join(outdir, '.toml');
  fs.writeFileSync(otherConfig, 'real non-target content');
  configureToml();
  jest.isolateModules(() => {
    const fixtureFs: typeof fs = require('fs');
    expect(fixtureFs.readFileSync(otherConfig, 'utf8')).toBe(
      'real non-target content',
    );
    expect(fixtureFs.readFileSync(otherConfig)).toEqual(
      Buffer.from('real non-target content'),
    );
    expect(() =>
      fixtureFs.readFileSync(path.join(outdir, 'missing.toml'), 'utf8'),
    ).toThrow('ENOENT');
  });
});

/** Load configuration afresh so each case exercises module-time validation. */
function loadConfig(): typeof import('../config/loader').Config {
  let config!: typeof import('../config/loader').Config;
  jest.isolateModules(() => {
    config = require('../config/loader.ts').Config;
  });
  return config;
}

test('omitting the digest fails configuration and the real bin path', () => {
  configureToml();
  expect(loadConfig).toThrow('HEALTHCARE_IMAGE_DIGEST');
  expect(() => jest.isolateModules(() => require('../bin/infra.ts'))).toThrow(
    'HEALTHCARE_IMAGE_DIGEST',
  );
});

test('an environment label cannot reuse a stale TOML digest', () => {
  configureToml(TOML_DIGEST);
  process.env.HEALTHCARE_IMAGE_TAG = 'new-build';
  expect(loadConfig).toThrow('HEALTHCARE_IMAGE_DIGEST');
});

test('TOML accepts an immutable digest while keeping the revision label', () => {
  configureToml(TOML_DIGEST);
  expect(loadConfig().healthcare).toMatchObject({
    imageDigest: TOML_DIGEST,
    imageTag: 'build-label',
  });
});

test('digest and tag environment overrides resolve independently', () => {
  configureToml(TOML_DIGEST);
  process.env.HEALTHCARE_IMAGE_DIGEST = ENV_DIGEST;
  process.env.HEALTHCARE_IMAGE_TAG = 'override-label';
  expect(loadConfig().healthcare).toMatchObject({
    imageDigest: ENV_DIGEST,
    imageTag: 'override-label',
  });
});

test('a digest does not replace the required deployed revision label', () => {
  configureToml(TOML_DIGEST, null);
  expect(loadConfig).toThrow('HEALTHCARE_IMAGE_TAG');
});

const malformedDigests = [
  '',
  'latest',
  `sha256:${'a'.repeat(63)}`,
  `sha256:${'a'.repeat(65)}`,
  `sha256:${'g'.repeat(64)}`,
  `sha256:${'A'.repeat(64)}`,
  `sha512:${'a'.repeat(64)}`,
  `repository@${TOML_DIGEST}`,
  `${TOML_DIGEST}\n`,
  ` ${TOML_DIGEST}`,
];

test.each([...malformedDigests, 123])(
  'rejects malformed TOML digest %j',
  (digest) => {
    configureToml(digest);
    expect(loadConfig).toThrow('Config validation error');
  },
);

test.each(malformedDigests)(
  'rejects malformed environment digest without falling back: %j',
  (digest) => {
    configureToml(TOML_DIGEST);
    process.env.HEALTHCARE_IMAGE_DIGEST = digest;
    expect(loadConfig).toThrow('HEALTHCARE_IMAGE_DIGEST');
  },
);

test.each([
  {
    name: 'TOML pin',
    digest: TOML_DIGEST,
    override: undefined,
    suffix: `@${TOML_DIGEST}`,
  },
  {
    name: 'environment pin',
    digest: TOML_DIGEST,
    override: ENV_DIGEST,
    suffix: `@${ENV_DIGEST}`,
  },
])(
  'real bin → loader → stack wiring: $name',
  ({ digest, override, suffix }) => {
    configureToml(digest);
    if (override !== undefined) process.env.HEALTHCARE_IMAGE_DIGEST = override;
    // Execute the real entry point and all stacks. App.synth writes only local
    // templates; no CDK CLI context provider or AWS client is invoked.
    jest.isolateModules(() => {
      require('../bin/infra.ts');
    });
    const template = JSON.parse(
      fs.readFileSync(
        path.join(outdir, 'RcaAgentDevHealthcareServiceStack.template.json'),
        'utf8',
      ),
    );
    const task = Object.values(template.Resources).find(
      (resource) =>
        (resource as { Type: string }).Type === 'AWS::ECS::TaskDefinition',
    ) as {
      Properties: {
        ContainerDefinitions: {
          Name: string;
          Image: unknown;
          Environment: { Name: string; Value: string }[];
        }[];
      };
    };
    const container = task.Properties.ContainerDefinitions.find(
      (candidate) => candidate.Name === 'healthcare',
    )!;
    expect(container.Image).toEqual({
      'Fn::Join': [
        '',
        [
          { Ref: 'AWS::AccountId' },
          '.dkr.ecr.',
          { Ref: 'AWS::Region' },
          `.amazonaws.com/rcaagentdev/healthcare${suffix}`,
        ],
      ],
    });
    expect(container.Environment).toContainEqual({
      Name: 'DEPLOYED_REVISION',
      Value: 'build-label',
    });
  },
);
