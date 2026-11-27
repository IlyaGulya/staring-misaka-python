import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { Schema } from 'effect'
import type { LlmProvider, Settings } from './domain.ts'

const ReasoningEffort = Schema.Literals(['none', 'minimal', 'low', 'medium', 'high'])

const SpamConfig = Schema.Struct({
  system_prompt: Schema.String,
  provider: Schema.optional(Schema.Literals(['openai', 'anthropic'])),
  model: Schema.optional(Schema.String),
  reasoning_effort: Schema.optional(ReasoningEffort),
  chats: Schema.optional(
    Schema.Record(
      Schema.String,
      Schema.Struct({
        name: Schema.optional(Schema.String),
        system_prompt: Schema.optional(Schema.String),
        extra_instructions: Schema.optional(Schema.String),
      }),
    ),
  ),
})

function integer(value: string | undefined, name: string, fallback?: number): number {
  if (value === undefined || value === '') {
    if (fallback !== undefined) return fallback
    throw new Error(`${name} is required`)
  }
  const number = Number(value)
  if (!/^-?\d+$/.test(value) || !Number.isSafeInteger(number)) {
    throw new Error(`${name} must be a safe integer`)
  }
  return number
}

function positive(value: string | undefined, name: string, fallback: number): number {
  const number = integer(value, name, fallback)
  if (number <= 0) throw new Error(`${name} must be positive`)
  return number
}

function nonNegativeInteger(value: string | undefined, name: string, fallback: number): number {
  const number = integer(value, name, fallback)
  if (number < 0) throw new Error(`${name} must be non-negative`)
  return number
}

function nonNegativeFloat(value: string | undefined, name: string, fallback: number): number {
  if (value === undefined || value === '') return fallback
  const number = Number(value)
  if (!Number.isFinite(number) || number < 0) {
    throw new Error(`${name} must be a finite non-negative number`)
  }
  return number
}

function booleanFlag(value: string | undefined, fallback: boolean): boolean {
  if (value === undefined || value === '') return fallback
  const normalized = value.trim().toLowerCase()
  if (normalized === 'true' || normalized === '1' || normalized === 'yes') return true
  if (normalized === 'false' || normalized === '0' || normalized === 'no') return false
  throw new Error(`Invalid boolean flag: ${value}`)
}

function endpoint(value: string, name: string): string {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new Error(`${name} must be a valid URL`)
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error(`${name} must not contain credentials, query or fragment`)
  }
  if (
    url.protocol !== 'https:' &&
    !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))
  ) {
    throw new Error(
      `${name} requires HTTPS (HTTP is allowed only on loopback for local verification)`,
    )
  }
  return value.replace(/\/$/, '')
}

function defaultRates(
  provider: LlmProvider,
  model: string,
): { input: number; output: number; cached: number } {
  const lower = model.toLowerCase()
  if (provider === 'openai') {
    if (lower.includes('luna')) return { input: 0.1, output: 0.5, cached: 0.01 }
    if (lower.includes('terra')) return { input: 1.0, output: 5.0, cached: 0.05 }
    if (lower.includes('sol')) return { input: 2.0, output: 10.0, cached: 0.1 }
    return { input: 0.5, output: 2.0, cached: 0.05 }
  }
  if (lower.includes('haiku')) return { input: 1.0, output: 5.0, cached: 0.1 }
  if (lower.includes('opus')) return { input: 15.0, output: 75.0, cached: 1.5 }
  return { input: 3.0, output: 15.0, cached: 0.3 }
}

export function loadSettings(
  env: Record<string, string | undefined> = process.env,
  requireSecrets = true,
): Settings {
  const file = resolve(env.SPAM_CONFIG_PATH ?? '../config.yaml')
  let yaml: typeof SpamConfig.Type
  try {
    const contents = readFileSync(file, 'utf8')
    yaml = Schema.decodeUnknownSync(SpamConfig)(Bun.YAML.parse(contents))
  } catch {
    // Parser errors can include source contents. Do not expose configuration secrets.
    throw new Error(
      'Invalid spam YAML: expected system_prompt, optional model and per-chat prompts',
    )
  }
  if (!yaml.system_prompt.trim()) throw new Error('system_prompt is required in spam YAML')
  const chats = yaml.chats ?? {}
  const configuredIds = env.TRACKING_CHAT_IDS?.trim() || Object.keys(chats).join(',')
  const chatIds = configuredIds
    .split(',')
    .filter(Boolean)
    .map((value) => integer(value.trim(), 'TRACKING_CHAT_IDS'))
  if (!chatIds.length || chatIds.some((id) => id >= 0)) {
    throw new Error('TRACKING_CHAT_IDS must contain group/channel IDs')
  }
  const chatPrompts: Record<string, string> = {}
  const chatNames: Record<string, string> = {}
  for (const [id, entry] of Object.entries(chats)) {
    const system = entry.system_prompt ?? yaml.system_prompt
    chatPrompts[id] = system + (entry.extra_instructions ? `\n\n${entry.extra_instructions}` : '')
    if (entry.name?.trim()) chatNames[id] = entry.name.trim()
  }
  const mode = env.MODE ?? 'shadow'
  if (mode !== 'shadow' && mode !== 'live') throw new Error('MODE must be shadow or live')

  const rawProvider = env.LLM_PROVIDER?.trim().toLowerCase() || yaml.provider
  let provider: LlmProvider
  if (rawProvider === 'openai' || rawProvider === 'anthropic') {
    provider = rawProvider
  } else if (rawProvider) {
    throw new Error('LLM_PROVIDER must be openai or anthropic')
  } else if (env.OPENAI_API_KEY && !env.ANTHROPIC_API_KEY) {
    provider = 'openai'
  } else if (env.ANTHROPIC_API_KEY) {
    provider = 'anthropic'
  } else {
    provider = 'openai'
  }

  const openaiKey = env.OPENAI_API_KEY ?? ''
  const anthropicKey = env.ANTHROPIC_API_KEY ?? ''
  if (requireSecrets) {
    if (!env.BOT_TOKEN) throw new Error('BOT_TOKEN is required')
    if (provider === 'openai' && !openaiKey) {
      throw new Error('OPENAI_API_KEY is required when LLM_PROVIDER=openai')
    }
    if (provider === 'anthropic' && !anthropicKey) {
      throw new Error('ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic')
    }
  }

  const rawEffort = env.OPENAI_REASONING_EFFORT?.trim().toLowerCase() || yaml.reasoning_effort
  const reasoningEffort = rawEffort ?? 'low'
  if (!Schema.is(ReasoningEffort)(reasoningEffort)) {
    throw new Error('OPENAI_REASONING_EFFORT must be none, minimal, low, medium, or high')
  }

  const model =
    env.LLM_MODEL?.trim() ||
    (provider === 'openai' ? 'gpt-6-luna' : yaml.model?.trim() || 'claude-sonnet-4-5-20250929')
  const rates = defaultRates(provider, model)

  const adminId = integer(env.ADMIN_ID, 'ADMIN_ID', requireSecrets ? undefined : 1)
  if (adminId <= 0) throw new Error('ADMIN_ID must be a positive user ID')
  const observerKeys = [env.OBSERVER_API_ID, env.OBSERVER_API_HASH, env.OBSERVER_SESSION]
  const configuredObserver = observerKeys.some(Boolean)
  if (configuredObserver && !observerKeys.every(Boolean)) {
    throw new Error('OBSERVER_API_ID, OBSERVER_API_HASH and OBSERVER_SESSION must be set together')
  }
  if (requireSecrets && mode === 'live') {
    if (env.ALLOW_LIVE_MODERATION !== 'true') {
      throw new Error('Live mode requires ALLOW_LIVE_MODERATION=true')
    }
    if (!configuredObserver && env.ACKNOWLEDGE_OBSERVER_GAP !== 'true') {
      throw new Error('Live mode requires an observer or explicit ACKNOWLEDGE_OBSERVER_GAP=true')
    }
  }
  const dbPath = resolve(env.DB_PATH ?? './data/misaka.sqlite')
  const healthPort = nonNegativeInteger(env.HEALTH_PORT, 'HEALTH_PORT', 0)
  if (healthPort > 65535) throw new Error('HEALTH_PORT must be <= 65535')

  return {
    mode,
    dbPath,
    botToken: env.BOT_TOKEN ?? '',
    adminId,
    chatIds: [...new Set(chatIds)],
    provider,
    openaiKey,
    openaiUrl: endpoint(env.OPENAI_BASE_URL ?? 'https://api.openai.com/v1', 'OPENAI_BASE_URL'),
    reasoningEffort,
    maxOutputTokens: positive(
      env.OPENAI_MAX_OUTPUT_TOKENS,
      'OPENAI_MAX_OUTPUT_TOKENS',
      provider === 'openai' ? 4096 : 1024,
    ),
    anthropicKey,
    anthropicUrl: endpoint(
      env.ANTHROPIC_BASE_URL ?? 'https://api.anthropic.com',
      'ANTHROPIC_BASE_URL',
    ),
    model,
    systemPrompt: yaml.system_prompt,
    chatPrompts,
    chatNames,
    telegramApiRoot: endpoint(
      env.TELEGRAM_API_ROOT ?? 'https://api.telegram.org',
      'TELEGRAM_API_ROOT',
    ),
    pollTimeout: positive(env.POLL_TIMEOUT, 'POLL_TIMEOUT', 25),
    concurrency: positive(env.CLASSIFICATION_CONCURRENCY, 'CLASSIFICATION_CONCURRENCY', 3),
    followupTtlMs: positive(env.FOLLOWUP_TTL_SECONDS, 'FOLLOWUP_TTL_SECONDS', 60) * 1000,
    classificationTimeoutMs:
      positive(env.CLASSIFICATION_TIMEOUT_SECONDS, 'CLASSIFICATION_TIMEOUT_SECONDS', 30) * 1000,
    maxClassificationAttempts: positive(
      env.MAX_CLASSIFICATION_ATTEMPTS,
      'MAX_CLASSIFICATION_ATTEMPTS',
      3,
    ),
    maxActionAttempts: positive(env.MAX_ACTION_ATTEMPTS, 'MAX_ACTION_ATTEMPTS', 5),
    retryBaseMs: positive(env.RETRY_BASE_SECONDS, 'RETRY_BASE_SECONDS', 5) * 1000,
    recoveryIntervalMs:
      positive(env.RECOVERY_INTERVAL_SECONDS, 'RECOVERY_INTERVAL_SECONDS', 300) * 1000,
    journalPath: resolve(env.JOURNAL_PATH ?? './data/events.jsonl'),
    dailyBudgetUsd: nonNegativeFloat(env.DAILY_BUDGET_USD, 'DAILY_BUDGET_USD', 5),
    inputUsdPerMillion: nonNegativeFloat(
      env.LLM_INPUT_USD_PER_MILLION,
      'LLM_INPUT_USD_PER_MILLION',
      rates.input,
    ),
    outputUsdPerMillion: nonNegativeFloat(
      env.LLM_OUTPUT_USD_PER_MILLION,
      'LLM_OUTPUT_USD_PER_MILLION',
      rates.output,
    ),
    cachedInputUsdPerMillion: nonNegativeFloat(
      env.LLM_CACHED_INPUT_USD_PER_MILLION,
      'LLM_CACHED_INPUT_USD_PER_MILLION',
      rates.cached,
    ),
    reputationThreshold: nonNegativeInteger(env.REPUTATION_THRESHOLD, 'REPUTATION_THRESHOLD', 5),
    reputationMinChars: positive(env.REPUTATION_MIN_CHARS, 'REPUTATION_MIN_CHARS', 30),
    banCleanupWindowMs:
      positive(env.BAN_CLEANUP_WINDOW_SECONDS, 'BAN_CLEANUP_WINDOW_SECONDS', 86_400) * 1000,
    retentionDays: nonNegativeInteger(env.RETENTION_DAYS, 'RETENTION_DAYS', 30),
    healthPort,
    healthHost: env.HEALTH_HOST?.trim() || '127.0.0.1',
    adminBanNotifications: booleanFlag(env.ADMIN_BAN_NOTIFICATIONS, true),
    adminAlerts: booleanFlag(env.ADMIN_ALERTS, true),
    observer: configuredObserver
      ? {
          apiId: positive(env.OBSERVER_API_ID, 'OBSERVER_API_ID', 0),
          apiHash: env.OBSERVER_API_HASH!,
          session: env.OBSERVER_SESSION!,
        }
      : undefined,
  }
}
