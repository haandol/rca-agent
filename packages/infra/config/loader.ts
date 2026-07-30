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
  // imageTag 에 기본값을 두지 않는다. 기본값이 있으면 태그를 주입하지 않은 배포가
  // 조용히 그 값으로 되돌아가는데, 가변 태그를 기본값으로 두면 되돌아가는 대상이
  // "언젠가 푸시된 이미지"가 되어 배포된 코드를 태그로 식별할 수 없게 된다.
  agent: z.object({ imageTag: z.string().optional() }),
  healthcare: z.object({ imageTag: z.string().optional() }),
  ccHeadless: z.object({ imageTag: z.string().optional() }),
  execution: z.object({ imageTag: z.string().optional() }),
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

type IRawConfig = z.infer<typeof ConfigSchema>;

// 스키마에서는 imageTag 가 없을 수 있지만, 해석을 통과한 설정에는 반드시 있다 —
// 없으면 imageTag For 가 던져서 여기까지 오지 못한다. 스택이 `imageTag!` 같은
// 단정을 쓰지 않도록 해석 결과의 타입으로 그 사실을 표현한다.
type IServiceImage = { readonly imageTag: string };
type IConfig = Omit<
  IRawConfig,
  'agent' | 'healthcare' | 'ccHeadless' | 'execution'
> & {
  readonly agent: IServiceImage;
  readonly healthcare: IServiceImage;
  readonly ccHeadless: IServiceImage;
  readonly execution: IServiceImage;
};

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
  execution: 'EXECUTION_IMAGE_TAG',
} as const;

/**
 * 이미지 태그를 확정한다. 확정할 수 없으면 synth 를 실패시킨다.
 *
 * CDK 는 배포 대상이 의존하는 스택을 함께 갱신한다. 그래서 한 서비스의 태그만
 * 주입하고 배포하면, 함께 갱신되는 다른 서비스의 태스크 정의가 조용히 다른 태그로
 * 바뀐다. 여기서 던지지 않고 어떤 값으로든 대체하면 그 사고가 배포 성공으로 끝나고,
 * 잘못된 이미지가 뜬 뒤에야 컨테이너 기동 실패로 드러난다.
 */
function imageTagFor(service: keyof typeof IMAGE_TAG_ENV_KEYS): string {
  const envKey = IMAGE_TAG_ENV_KEYS[service];
  const resolved = process.env[envKey] || parsed[service].imageTag;
  if (!resolved) {
    throw new Error(
      `${service} 이미지 태그가 없습니다. ${envKey} 환경변수를 주입하거나 ` +
        `.toml 의 [${service}] 섹션에 imageTag 를 지정하세요. ` +
        `배포는 'pnpm --filter infra run deploy:service -- <service>' 로 하면 ` +
        `커밋 SHA 태그가 자동 주입됩니다.`,
    );
  }
  return resolved;
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
  execution: { ...parsed.execution, imageTag: imageTagFor('execution') },
};
