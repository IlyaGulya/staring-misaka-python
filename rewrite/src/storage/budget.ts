import type { Settings, UsageSummary } from '../domain.ts'
import type { StorageContext } from './context.ts'

export function utcDay(nowMs: number): string {
  return new Date(nowMs).toISOString().slice(0, 10)
}

/** Estimate USD cost from reported token usage without double-counting cache/reasoning subsets. */
export function estimateCostUsd(
  settings: Pick<
    Settings,
    'inputUsdPerMillion' | 'outputUsdPerMillion' | 'cachedInputUsdPerMillion'
  >,
  usage: Record<string, number> | undefined,
): {
  readonly inputTokens: number
  readonly outputTokens: number
  readonly cachedTokens: number
  readonly estimatedUsd: number
} {
  if (!usage) {
    return { inputTokens: 0, outputTokens: 0, cachedTokens: 0, estimatedUsd: 0 }
  }
  const inputRate = settings.inputUsdPerMillion ?? 0.1
  const outputRate = settings.outputUsdPerMillion ?? 0.5
  const cachedRate = settings.cachedInputUsdPerMillion ?? inputRate * 0.1

  // OpenAI reports total input_tokens with cached_input_tokens and cache_write_tokens as subsets.
  // Anthropic reports uncached input_tokens separately from cache_read/cache_creation.
  const isOpenAIShape =
    usage.cached_input_tokens !== undefined ||
    usage.cache_write_tokens !== undefined ||
    usage.reasoning_tokens !== undefined

  const outputTokens = Math.max(0, usage.output_tokens ?? 0)
  if (isOpenAIShape) {
    const totalInput = Math.max(0, usage.input_tokens ?? 0)
    const cached = Math.min(totalInput, Math.max(0, usage.cached_input_tokens ?? 0))
    const cacheWrite = Math.min(totalInput - cached, Math.max(0, usage.cache_write_tokens ?? 0))
    const uncached = Math.max(0, totalInput - cached - cacheWrite)
    const estimatedUsd =
      (uncached * inputRate +
        cached * cachedRate +
        cacheWrite * inputRate * 1.25 +
        outputTokens * outputRate) /
      1_000_000
    return { inputTokens: totalInput, outputTokens, cachedTokens: cached, estimatedUsd }
  }

  const uncached = Math.max(0, usage.input_tokens ?? 0)
  const cacheRead = Math.max(0, usage.cache_read_input_tokens ?? 0)
  const cacheWrite = Math.max(0, usage.cache_creation_input_tokens ?? 0)
  const totalInput = uncached + cacheRead + cacheWrite
  const estimatedUsd =
    (uncached * inputRate +
      cacheRead * cachedRate +
      cacheWrite * inputRate * 1.25 +
      outputTokens * outputRate) /
    1_000_000
  return { inputTokens: totalInput, outputTokens, cachedTokens: cacheRead, estimatedUsd }
}

export function recordUsage(
  context: StorageContext,
  chatId: number,
  usage: Record<string, number> | undefined,
): number {
  const canonical = context.canonicalChatId(chatId)
  const day = utcDay(context.now())
  const cost = estimateCostUsd(context.settings, usage)
  context.run(
    `
    INSERT INTO llm_usage(day, chatId, requests, inputTokens, outputTokens, cachedTokens, estimatedUsd)
    VALUES(?, ?, 1, ?, ?, ?, ?)
    ON CONFLICT(day, chatId) DO UPDATE SET
      requests = requests + 1,
      inputTokens = inputTokens + excluded.inputTokens,
      outputTokens = outputTokens + excluded.outputTokens,
      cachedTokens = cachedTokens + excluded.cachedTokens,
      estimatedUsd = estimatedUsd + excluded.estimatedUsd
  `,
    day,
    canonical,
    cost.inputTokens,
    cost.outputTokens,
    cost.cachedTokens,
    cost.estimatedUsd,
  )
  return cost.estimatedUsd
}

export function getTodayUsage(context: StorageContext, chatId?: number): UsageSummary {
  const day = utcDay(context.now())
  const row =
    chatId === undefined
      ? context.get<{
          requests: number
          inputTokens: number
          outputTokens: number
          cachedTokens: number
          estimatedUsd: number
        }>(
          `
          SELECT
            COALESCE(SUM(requests), 0) AS requests,
            COALESCE(SUM(inputTokens), 0) AS inputTokens,
            COALESCE(SUM(outputTokens), 0) AS outputTokens,
            COALESCE(SUM(cachedTokens), 0) AS cachedTokens,
            COALESCE(SUM(estimatedUsd), 0) AS estimatedUsd
          FROM llm_usage
          WHERE day = ?
        `,
          day,
        )
      : context.get<{
          requests: number
          inputTokens: number
          outputTokens: number
          cachedTokens: number
          estimatedUsd: number
        }>(
          `
          SELECT
            COALESCE(requests, 0) AS requests,
            COALESCE(inputTokens, 0) AS inputTokens,
            COALESCE(outputTokens, 0) AS outputTokens,
            COALESCE(cachedTokens, 0) AS cachedTokens,
            COALESCE(estimatedUsd, 0) AS estimatedUsd
          FROM llm_usage
          WHERE day = ? AND chatId = ?
        `,
          day,
          context.canonicalChatId(chatId),
        )
  return {
    day,
    requests: row?.requests ?? 0,
    inputTokens: row?.inputTokens ?? 0,
    outputTokens: row?.outputTokens ?? 0,
    cachedTokens: row?.cachedTokens ?? 0,
    estimatedUsd: row?.estimatedUsd ?? 0,
  }
}

export function isDailyBudgetExceeded(context: StorageContext): boolean {
  const cap = context.settings.dailyBudgetUsd ?? 0
  if (cap <= 0) return false
  return getTodayUsage(context).estimatedUsd >= cap
}
