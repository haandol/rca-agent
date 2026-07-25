import * as path from 'path';
import * as fs from 'fs';
import * as toml from 'toml';
import { z } from 'zod/v4';

const ConfigSchema = z.object({
  app: z
    .object({
      ns: z.string(),
      stage: z.string(),
    })
    .required(),
  aws: z
    .object({
      region: z.string(),
    })
    .required(),
  alarm: z
    .object({
      notificationEmail: z.string(),
    })
    .required(),
  agent: z
    .object({
      imageTag: z.string().default('latest'),
    })
    .required(),
  healthcare: z
    .object({
      imageTag: z.string().default('latest'),
    })
    .required(),
  ccHeadless: z
    .object({
      imageTag: z.string().default('latest'),
    })
    .required(),
  remediation: z
    .object({
      imageTag: z.string().default('latest'),
      desiredCount: z.number().int().min(0).max(1).default(0),
    })
    .required(),
  storage: z
    .object({
      evidenceBucket: z.string(),
      vectorBucket: z.string(),
    })
    .required(),
  table: z
    .object({
      rcaSession: z
        .object({
          name: z.string(),
        })
        .required(),
    })
    .required(),
  tracing: z
    .object({
      enabled: z.boolean().default(false),
    })
    .required(),
});

type IConfig = z.infer<typeof ConfigSchema>;

const cfg = toml.parse(
  fs.readFileSync(path.resolve(__dirname, '..', '.toml'), 'utf-8'),
);

const result = ConfigSchema.safeParse(cfg);
if (!result.success) {
  throw new Error(`Config validation error: ${result.error.message}`);
}

const parsed = result.data;

// 배포 스크립트가 방금 빌드·푸시한 불변 태그를 주입한다. 태스크 정의가 그 태그를
// 직접 가리켜야 실행 중인 코드와 하네스 버전을 태그만으로 식별할 수 있다.
const IMAGE_TAG_ENV_KEYS = {
  agent: 'AGENT_IMAGE_TAG',
  healthcare: 'HEALTHCARE_IMAGE_TAG',
  ccHeadless: 'CC_HEADLESS_IMAGE_TAG',
  remediation: 'REMEDIATION_IMAGE_TAG',
} as const;

function imageTagFor(service: keyof typeof IMAGE_TAG_ENV_KEYS): string {
  const override = process.env[IMAGE_TAG_ENV_KEYS[service]];
  return override && override.length > 0 ? override : parsed[service].imageTag;
}

export const Config: IConfig = {
  ...parsed,
  app: {
    ...parsed.app,
    ns: `${parsed.app.ns}${parsed.app.stage}`,
  },
  agent: { ...parsed.agent, imageTag: imageTagFor('agent') },
  healthcare: { ...parsed.healthcare, imageTag: imageTagFor('healthcare') },
  ccHeadless: { ...parsed.ccHeadless, imageTag: imageTagFor('ccHeadless') },
  remediation: {
    ...parsed.remediation,
    imageTag: imageTagFor('remediation'),
  },
};
