import { BunRuntime } from '@effect/platform-bun'
import { Effect } from 'effect'
import { application } from './application.ts'
import { loadSettings } from './config.ts'
import { failure } from './domain.ts'
import { withTelemetry } from './telemetry.ts'

const HELP = `Staring Misaka — Bun / Effect 4 / grammY

bun run start                 Start durable receiver and workers (MODE=shadow by default)
bun run start --check-config  Validate configuration without network or database writes
bun run import --help        Import a legacy Python SQLite database
bun run observer-login       Create an MTProto observer session interactively`

if (import.meta.main) {
  if (process.argv.includes('--help')) {
    console.log(HELP)
  } else if (process.argv.includes('--check-config')) {
    const settings = loadSettings(process.env, false)
    const provider = settings.provider ?? 'anthropic'
    const providerKeyPresent =
      provider === 'openai' ? Boolean(settings.openaiKey) : Boolean(settings.anthropicKey)
    console.log(
      JSON.stringify(
        {
          mode: settings.mode,
          provider,
          model: settings.model,
          ...(provider === 'openai' ? { reasoningEffort: settings.reasoningEffort ?? 'low' } : {}),
          chats: settings.chatIds,
          chatNames: settings.chatNames ?? {},
          observer: Boolean(settings.observer),
          dailyBudgetUsd: settings.dailyBudgetUsd ?? 0,
          reputationThreshold: settings.reputationThreshold ?? 0,
          retentionDays: settings.retentionDays ?? 0,
          healthPort: settings.healthPort ?? 0,
          credentialsConfigured: Boolean(settings.botToken && providerKeyPresent),
        },
        null,
        2,
      ),
    )
  } else {
    const program = Effect.try({ try: () => loadSettings(), catch: failure }).pipe(
      Effect.flatMap(application),
    )
    BunRuntime.runMain(withTelemetry(program, process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT))
  }
}
