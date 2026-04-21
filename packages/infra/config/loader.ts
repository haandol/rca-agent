import * as path from 'path'
import * as fs from 'fs'
import * as toml from 'toml'
import { z } from 'zod/v4'

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
})

type IConfig = z.infer<typeof ConfigSchema>

const cfg = toml.parse(fs.readFileSync(path.resolve(__dirname, '..', '.toml'), 'utf-8'))

const result = ConfigSchema.safeParse(cfg)
if (!result.success) {
  throw new Error(`Config validation error: ${result.error.message}`)
}

export const Config: IConfig = {
  ...result.data,
  app: {
    ...result.data.app,
    ns: `${result.data.app.ns}${result.data.app.stage}`,
  },
}
