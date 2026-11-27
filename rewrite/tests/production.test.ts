import assert from 'node:assert/strict'
import { afterEach, describe, expect, test } from 'bun:test'
import { Database } from 'bun:sqlite'
import { Effect, Fiber } from 'effect'
import { Api } from 'grammy'
import { mkdtempSync, existsSync, rmSync, utimesSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { SpamClassifier } from '../src/classifier.ts'
import { handleCommand } from '../src/commands.ts'
import { loadSettings } from '../src/config.ts'
import {
  type Observation,
  type RuntimeStatus,
  type Settings,
  type Snapshot,
  type TelegramPort,
} from '../src/domain.ts'
import { persistImage } from '../src/media.ts'
import { runAdminAlertCycle } from '../src/runtime/alerts.ts'
import { startHealthServer } from '../src/runtime/health.ts'
import { RuntimeLog } from '../src/runtime/log.ts'
import { estimateCostUsd } from '../src/storage/budget.ts'
import { assessRevisionRisk, isSubstantiveMessage } from '../src/storage/reputation.ts'
import { Store } from '../src/store.ts'

const directories: string[] = []
const stores = new Set<Store>()
const servers: Array<{ close(): Promise<void> }> = []

afterEach(async () => {
  for (const server of servers.splice(0)) await server.close()
  for (const store of stores) store.close()
  stores.clear()
  for (const dir of directories.splice(0)) rmSync(dir, { recursive: true, force: true })
})

function makeSettings(overrides: Partial<Settings> = {}): Settings {
  const dir = mkdtempSync(join(tmpdir(), 'misaka-prod-'))
  directories.push(dir)
  return {
    mode: 'live',
    dbPath: join(dir, 'misaka.sqlite'),
    journalPath: join(dir, 'events.jsonl'),
    botToken: '123456:test-bot-secret',
    adminId: 99,
    chatIds: [-100],
    provider: 'openai',
    openaiKey: 'sk-test-openai-secret-1234567890',
    openaiUrl: 'https://api.openai.com/v1',
    reasoningEffort: 'low',
    maxOutputTokens: 4096,
    anthropicKey: '',
    anthropicUrl: 'https://api.anthropic.com',
    model: 'gpt-6-luna',
    systemPrompt: 'Detect spam',
    chatPrompts: { '-100': 'Mobile DI policy' },
    chatNames: { '-100': 'Mobile DI' },
    telegramApiRoot: 'https://api.telegram.org',
    pollTimeout: 25,
    concurrency: 1,
    followupTtlMs: 60_000,
    classificationTimeoutMs: 10_000,
    maxClassificationAttempts: 3,
    maxActionAttempts: 5,
    retryBaseMs: 1_000,
    recoveryIntervalMs: 30_000,
    dailyBudgetUsd: 5,
    inputUsdPerMillion: 0.1,
    outputUsdPerMillion: 0.5,
    cachedInputUsdPerMillion: 0.01,
    reputationThreshold: 3,
    reputationMinChars: 30,
    banCleanupWindowMs: 86_400_000,
    retentionDays: 30,
    healthPort: 0,
    healthHost: '127.0.0.1',
    adminBanNotifications: true,
    adminAlerts: true,
    ...overrides,
  }
}

function openStore(settings: Settings, clock: { now: number }): Store {
  const store = new Store(settings, () => clock.now)
  stores.add(store)
  return store
}

function obs(messageId: number, extras: Partial<Observation> = {}): Observation {
  return {
    source: 'bot',
    sourceKey: `obs:${messageId}:${extras.kind ?? 'message'}:${extras.editedAt ?? 0}`,
    kind: 'message',
    chatId: -100,
    messageId,
    userId: 42,
    actorKind: 'user',
    username: 'regular_dev',
    displayName: 'Regular Dev',
    text: 'Это вполне осмысленное техническое сообщение про Dagger и зависимости в Android.',
    occurredAt: 1_700_000_000_000,
    ...extras,
  }
}

describe('OpenAI Responses API classifier', () => {
  test('sends strict Responses payload with causal context and rejects hallucinated context IDs', async () => {
    const receivedBodies: unknown[] = []
    let nextResponse = Response.json({})
    const server = Bun.serve({
      hostname: '127.0.0.1',
      port: 0,
      async fetch(req) {
        expect(req.headers.get('authorization')).toBe('Bearer sk-test-openai-secret-1234567890')
        receivedBodies.push(await req.json())
        return nextResponse
      },
    })
    servers.push({ close: () => server.stop(true) })

    const settings = makeSettings({ openaiUrl: `${server.url.origin}/v1` })
    const classifier = new SpamClassifier(settings, new Api(settings.botToken))
    const snapshot: Snapshot = {
      id: 'snap-1',
      chatId: -100,
      userId: 42,
      generation: 0,
      version: 0,
      context: [
        {
          messageId: 1,
          userId: 10,
          sameAuthor: false,
          isReplyParent: true,
          sender: 'Alice (@alice)',
          occurredAt: 1_700_000_000_000,
          text: 'Кто-нибудь использует metro?',
        },
      ],
      messages: [
        {
          messageId: 2,
          revision: 1,
          text: 'Пишите + в лс, плачу 500$ в день за простую удалёнку!',
          media: [],
          metadata: {},
        },
      ],
    }

    // Valid spam response citing target messageId 2
    nextResponse = Response.json({
      id: 'resp_1',
      status: 'completed',
      model: 'gpt-6-luna',
      output: [
        {
          type: 'message',
          content: [
            {
              type: 'output_text',
              text: JSON.stringify({
                verdict: 'spam',
                reason: 'Обещание нереалистичного заработка и призыв писать в ЛС',
                evidenceMessageIds: [2],
              }),
            },
          ],
        },
      ],
      usage: {
        input_tokens: 400,
        output_tokens: 45,
        input_tokens_details: { cached_tokens: 200, cache_write_tokens: 0 },
        output_tokens_details: { reasoning_tokens: 15 },
      },
    })

    const result = await Effect.runPromise(classifier.classify(snapshot))
    expect(result.verdict).toBe('spam')
    expect(result.evidenceMessageIds).toEqual([2])
    expect(result.usage).toEqual({
      input_tokens: 400,
      output_tokens: 45,
      cached_input_tokens: 200,
      cache_write_tokens: 0,
      reasoning_tokens: 15,
    })
    expect(receivedBodies[0]).toMatchObject({
      model: 'gpt-6-luna',
      store: false,
      tools: [],
      truncation: 'disabled',
      reasoning: { mode: 'standard', effort: 'low' },
    })
    const body = receivedBodies[0]
    assert(body && typeof body === 'object' && 'input' in body)
    const inputText = JSON.stringify(body.input)
    expect(inputText).toContain('prior_chat_context')
    expect(inputText).toContain('Кто-нибудь использует metro?')

    // Model citing context-only messageId 1 must be rejected as invalid!
    nextResponse = Response.json({
      id: 'resp_2',
      status: 'completed',
      model: 'gpt-6-luna',
      output: [
        {
          type: 'message',
          content: [
            {
              type: 'output_text',
              text: JSON.stringify({
                verdict: 'spam',
                reason: 'Wrongly cited context message',
                evidenceMessageIds: [1],
              }),
            },
          ],
        },
      ],
    })
    await assert.rejects(Effect.runPromise(classifier.classify(snapshot)), /invalid evidence/)

    // HTTP 429 preserves Retry-After and does not leak response body
    nextResponse = new Response('secret-upstream-error-body', {
      status: 429,
      headers: { 'retry-after': '12' },
    })
    const err = await Effect.runPromise(Effect.flip(classifier.classify(snapshot)))
    expect(err).toMatchObject({ category: 'transient', retryAfterMs: 12_000 })
    expect(err.message).not.toContain('secret-upstream-error-body')

    // HTTP 401 is permanent and never leaks key
    nextResponse = new Response('Invalid key sk-test-openai-secret-1234567890', { status: 401 })
    const authErr = await Effect.runPromise(Effect.flip(classifier.classify(snapshot)))
    expect(authErr).toMatchObject({ category: 'permanent', message: 'OpenAI HTTP 401' })

    // Refusal is rejected as invalid
    nextResponse = Response.json({
      id: 'resp_refusal',
      status: 'completed',
      model: 'gpt-6-luna',
      output: [{ type: 'message', content: [{ type: 'refusal', refusal: 'cannot comply' }] }],
    })
    const refusalErr = await Effect.runPromise(Effect.flip(classifier.classify(snapshot)))
    expect(refusalErr.category).toBe('invalid')

    nextResponse = Response.json({ status: 'completed', output: [null] })
    const malformed = await Effect.runPromise(Effect.flip(classifier.classify(snapshot)))
    expect(malformed).toMatchObject({
      category: 'invalid',
      message: 'OpenAI response failed schema validation',
    })
  })

  test('OpenAI request respects Effect interruption immediately', async () => {
    let release!: () => void
    const started = new Promise<void>((r) => (release = r))
    const server = Bun.serve({
      hostname: '127.0.0.1',
      port: 0,
      fetch(req) {
        release()
        return new Promise<Response>((_resolve, reject) => {
          req.signal.addEventListener('abort', () => reject(new Error('aborted')))
        })
      },
    })
    servers.push({ close: () => server.stop(true) })

    const settings = makeSettings({ openaiUrl: `${server.url.origin}/v1` })
    const classifier = new SpamClassifier(settings, new Api(settings.botToken))
    const fiber = Effect.runFork(
      classifier.classify({
        id: 's',
        chatId: -100,
        userId: 1,
        generation: 0,
        version: 0,
        messages: [{ messageId: 1, revision: 1, text: 'hi', media: [], metadata: {} }],
      }),
    )
    await started
    await Effect.runPromise(Fiber.interrupt(fiber))
  })
})

describe('Causal context, soft reputation, budget guard, and scoped cleanup', () => {
  test('snapshots include strictly past causal chat context (reply + same-author + recent) and never future messages', () => {
    const clock = { now: 1_700_000_100_000 }
    const store = openStore(makeSettings({ reputationThreshold: 0 }), clock)

    store.ingest(
      obs(1, {
        userId: 10,
        username: 'alice',
        displayName: 'Alice',
        text: 'Как настроить Koin в KMP?',
        occurredAt: clock.now - 60_000,
        metadata: { sender: { username: 'alice', firstName: 'Alice' } },
      }),
    )
    store.ingest(
      obs(2, {
        userId: 42,
        text: 'Всем привет!',
        occurredAt: clock.now - 30_000,
        metadata: { sender: { username: 'regular_dev', firstName: 'Regular Dev' } },
      }),
    )
    // Complete message 2 as clean so processedSeq advances past it
    const firstJob = store.claimClassification()!
    // First job is for user 10 (alice); complete it first
    store.completeClassification(firstJob, {
      verdict: 'clean',
      reason: 'On-topic',
      evidenceMessageIds: firstJob.snapshot.messages.map((m) => m.messageId),
      model: 'gpt-6-luna',
      promptVersion: 'v3',
    })
    const secondJob = store.claimClassification()!
    expect(secondJob.snapshot.userId).toBe(42)
    store.completeClassification(secondJob, {
      verdict: 'clean',
      reason: 'Greeting',
      evidenceMessageIds: [2],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
    })

    // Now user 42 replies to message 1
    store.ingest(
      obs(3, {
        userId: 42,
        text: 'Пишите мне в лс за заработком',
        occurredAt: clock.now,
        metadata: { replyToMessageId: 1 },
      }),
    )
    // A future message from user 11 arrives after message 3
    store.ingest(
      obs(4, {
        userId: 11,
        text: 'Будущее сообщение после target',
        occurredAt: clock.now + 5_000,
      }),
    )

    const thirdJob = store.claimClassification()!
    expect(thirdJob.snapshot.userId).toBe(42)
    expect(thirdJob.snapshot.messages.map((m) => m.messageId)).toEqual([3])
    const contextIds = (thirdJob.snapshot.context ?? []).map((c) => c.messageId)
    expect(contextIds).toEqual([1, 2])
    expect(contextIds).not.toContain(3)
    expect(contextIds).not.toContain(4)
    expect(thirdJob.snapshot.context![0]!.isReplyParent).toBe(true)
    expect(thirdJob.snapshot.context![1]!.sameAuthor).toBe(true)
  })

  test('soft reputation skips plain messages after threshold, but ALWAYS checks edits, links, media, obfuscation, and bursts', () => {
    expect(isSubstantiveMessage('привет!', 30)).toBe(false)
    expect(isSubstantiveMessage('....................................', 30)).toBe(false)
    expect(
      isSubstantiveMessage('Отличный вопрос про Hilt и ViewModel в многомодульном проекте', 30),
    ).toBe(true)

    expect(
      assessRevisionRisk({ revision: 2, text: 'Обычный текст', media: [], metadata: {} }, 1).risky,
    ).toBe(true)
    expect(
      assessRevisionRisk({ revision: 1, text: 'Заходите на t.me/scam', media: [], metadata: {} }, 1)
        .risky,
    ).toBe(true)
    expect(
      assessRevisionRisk(
        { revision: 1, text: 'Рaбoта онлайн', media: [], metadata: {} }, // mixed Cyrillic+Latin
        1,
      ).risky,
    ).toBe(true)
    expect(
      assessRevisionRisk({ revision: 1, text: 'Обычный текст', media: [], metadata: {} }, 4).risky,
    ).toBe(true)
    expect(
      assessRevisionRisk(
        { revision: 1, text: 'Обычный спокойный ответ', media: [], metadata: {} },
        1,
      ).risky,
    ).toBe(false)

    const clock = { now: 1_700_000_000_000 }
    const store = openStore(makeSettings({ reputationThreshold: 2, reputationMinChars: 30 }), clock)

    // 1. Short greeting does NOT earn reputation
    store.ingest(obs(1, { text: 'Привет всем!' }))
    let job = store.claimClassification()!
    store.completeClassification(job, {
      verdict: 'clean',
      reason: 'ok',
      evidenceMessageIds: [1],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
    })

    // 2 & 3. Two substantive clean messages earn reputation (threshold = 2)
    for (const id of [2, 3]) {
      clock.now += 60_000
      store.ingest(obs(id, { occurredAt: clock.now }))
      job = store.claimClassification()!
      store.completeClassification(job, {
        verdict: 'clean',
        reason: 'ok',
        evidenceMessageIds: [id],
        model: 'gpt-6-luna',
        promptVersion: 'v3',
      })
    }

    // 4. Plain unedited message from established regular skips LLM!
    clock.now += 60_000
    store.ingest(
      obs(4, {
        occurredAt: clock.now,
        text: 'Да, я тоже сталкивался с такой проблемой в Gradle вчера.',
      }),
    )
    expect(store.claimClassification()).toBeUndefined()

    // 5. Editing message 4 into spam ALWAYS triggers LLM!
    clock.now += 10_000
    store.ingest(
      obs(4, {
        kind: 'edit',
        editedAt: clock.now,
        text: 'Срочно нужны люди на удалёнку 300$/день пишите в лс',
      }),
    )
    const editJob = store.claimClassification()
    expect(editJob).toBeDefined()
    expect(editJob!.snapshot.messages[0]!.revision).toBe(2)
    store.completeClassification(editJob!, {
      verdict: 'clean',
      reason: 'test',
      evidenceMessageIds: [4],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
    })

    // 6. Message with link from established regular ALWAYS triggers LLM!
    clock.now += 60_000
    store.ingest(
      obs(5, {
        occurredAt: clock.now,
        text: 'Посмотрите вот эту ссылку https://example.com/promo',
      }),
    )
    expect(store.claimClassification()).toBeDefined()
  })

  test('daily budget guard pauses new classifications once cap is reached and resumes next UTC day', () => {
    const cost = estimateCostUsd(
      { inputUsdPerMillion: 0.1, outputUsdPerMillion: 0.5, cachedInputUsdPerMillion: 0.01 },
      { input_tokens: 1_000_000, output_tokens: 100_000, cached_input_tokens: 500_000 },
    )
    // 500k uncached ($0.05) + 500k cached ($0.005) + 100k output ($0.05) = $0.105
    expect(cost.estimatedUsd).toBeCloseTo(0.105, 6)

    const clock = { now: Date.UTC(2026, 10, 26, 12, 0, 0) }
    const store = openStore(makeSettings({ dailyBudgetUsd: 0.05, reputationThreshold: 0 }), clock)
    store.ingest(obs(1, { userId: 42 }))
    const job = store.claimClassification()!
    store.completeClassification(job, {
      verdict: 'clean',
      reason: 'ok',
      evidenceMessageIds: [1],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
      usage: { input_tokens: 600_000, output_tokens: 0, cached_input_tokens: 0 },
    })
    expect(store.todayUsage().estimatedUsd).toBeGreaterThanOrEqual(0.05)
    expect(store.isBudgetExceeded()).toBe(true)

    // Next user's message stays queued (NOT dropped or marked clean!)
    store.ingest(obs(2, { userId: 43 }))
    expect(store.claimClassification()).toBeUndefined()

    // Next UTC day resumes automatically
    clock.now = Date.UTC(2026, 10, 27, 0, 1, 0)
    expect(store.isBudgetExceeded()).toBe(false)
    expect(store.claimClassification()?.snapshot.userId).toBe(43)
  })

  test('scoped ban cleanup deletes spam episode + evidence, preserving multi-day legitimate history', () => {
    const clock = { now: 1_700_000_000_000 }
    const store = openStore(
      makeSettings({ banCleanupWindowMs: 86_400_000, reputationThreshold: 0 }),
      clock,
    )

    // Message from 5 days ago
    store.ingest(
      obs(1, { occurredAt: clock.now - 5 * 86_400_000, text: 'Старое полезное сообщение' }),
    )
    const oldJob = store.claimClassification()!
    store.completeClassification(oldJob, {
      verdict: 'clean',
      reason: 'ok',
      evidenceMessageIds: [1],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
    })

    // Recent messages (1 hour ago + now)
    store.ingest(obs(2, { occurredAt: clock.now - 3_600_000, text: 'Привет' }))
    store.ingest(obs(3, { occurredAt: clock.now, text: 'Заработок 500$ в день' }))
    const spamJob = store.claimClassification()!
    store.completeClassification(spamJob, {
      verdict: 'spam',
      reason: 'Скам-заработок',
      evidenceMessageIds: [3],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
    })

    const db = new Database(store.settings.dbPath, { readonly: true })
    try {
      const deletedIds = db
        .query<{ messageId: number }, []>(
          "SELECT messageId FROM actions WHERE kind='delete' ORDER BY messageId",
        )
        .all()
        .map((r) => r.messageId)
      expect(deletedIds).toEqual([2, 3])
      expect(deletedIds).not.toContain(1)
    } finally {
      db.close()
    }
  })
})

describe('Retention, health probes, admin alerts, and smart Russian UX', () => {
  test('retention prunes old raw observations, exported audit and unused media while protecting spam evidence', async () => {
    const clock = { now: 1_700_000_000_000 }
    const settings = makeSettings({ retentionDays: 7, reputationThreshold: 0 })
    const store = openStore(settings, clock)

    const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00])
    const unusedHash = await persistImage(settings.dbPath, png)
    const spamPng = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x01])
    const spamHash = await persistImage(settings.dbPath, spamPng)

    store.ingest(
      obs(1, {
        media: [{ kind: 'photo', blobHash: spamHash, mimeType: 'image/png' }],
        text: 'Спам с картинкой',
      }),
    )
    const job = store.claimClassification()!
    store.completeClassification(job, {
      verdict: 'spam',
      reason: 'Спам',
      evidenceMessageIds: [1],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
    })
    store.exportJournal()

    // Age both media files beyond 7 days
    const oldSec = Math.floor((clock.now - 10 * 86_400_000) / 1000)
    const mediaDir = join(settings.dbPath, '..', 'media')
    utimesSync(join(mediaDir, unusedHash), oldSec, oldSec)
    utimesSync(join(mediaDir, spamHash), oldSec, oldSec)

    // Advance clock 10 days
    clock.now += 10 * 86_400_000
    const report = store.runRetention()
    expect(report.prunedObservations).toBeGreaterThan(0)
    expect(report.prunedAudit).toBeGreaterThan(0)
    expect(report.prunedMedia).toBe(1)
    expect(existsSync(join(mediaDir, unusedHash))).toBe(false)
    expect(existsSync(join(mediaDir, spamHash))).toBe(true)
    // Decisions remain intact forever!
    expect(store.decisionsFor(42)).toHaveLength(1)
  })

  test('health server exposes /healthz and /readyz with zero secrets', async () => {
    const clock = { now: Date.now() }
    const settings = makeSettings({ healthPort: 0 })
    const store = openStore(settings, clock)
    const status: RuntimeStatus = {
      startedAt: Date.now() - 10_000,
      botId: 777,
      botUsername: 'misaka_bot',
      ready: false,
      lastPollAt: Date.now(),
      observerConfigured: true,
      observerConnected: true,
      observerLastEventAt: Date.now(),
      observerDisconnectedSince: null,
    }
    const server = startHealthServer({ ...settings, healthPort: 18943 }, store, () => status)!
    servers.push(server)

    const notReady = await fetch(`http://127.0.0.1:${server.port}/readyz`)
    expect(notReady.status).toBe(503)

    const healthy = await fetch(`http://127.0.0.1:${server.port}/healthz`)
    expect(healthy.status).toBe(200)
    const text = await healthy.text()
    expect(text).not.toContain(settings.botToken)
    expect(text).not.toContain(settings.openaiKey!)

    ;(status as { ready: boolean }).ready = true
    const ready = await fetch(`http://127.0.0.1:${server.port}/readyz`)
    expect(ready.status).toBe(200)
  })

  test('admin DM ban cards and deduplicated incident alerts fire once on open and once on resolve', async () => {
    const clock = { now: 1_700_000_000_000 }
    const settings = makeSettings({ dailyBudgetUsd: 0.01, reputationThreshold: 0 })
    const store = openStore(settings, clock)

    store.ingest(obs(1, { username: 'spammer_x', displayName: 'Spam X', text: 'Купи крипту!' }))
    const job = store.claimClassification()!
    store.completeClassification(job, {
      verdict: 'spam',
      reason: 'Крипто-реклама',
      evidenceMessageIds: [1],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
      usage: { input_tokens: 200_000, output_tokens: 0, cached_input_tokens: 0 },
    })

    const sent: Array<{
      chatId: number | string
      text: string
      options?: Record<string, unknown>
    }> = []
    const api: Parameters<typeof runAdminAlertCycle>[1] = {
      sendMessage: async (chatId, text, options) => {
        sent.push({ chatId, text, options })
        return { message_id: sent.length }
      },
    }
    const status: RuntimeStatus = {
      startedAt: clock.now,
      botId: 777,
      botUsername: 'misaka_bot',
      ready: true,
      lastPollAt: clock.now,
      observerConfigured: false,
      observerConnected: false,
      observerLastEventAt: 0,
      observerDisconnectedSince: null,
    }

    await runAdminAlertCycle(
      store,
      api,
      settings,
      status,
      new RuntimeLog(settings),
      new AbortController().signal,
    )
    // Should send 1 silent ban card + 1 budget_exceeded alert
    expect(sent).toHaveLength(2)
    expect(sent[0]!.chatId).toBe(99)
    expect(sent[0]!.options?.disable_notification).toBe(true)
    expect(sent[0]!.text).toContain('Mobile DI')
    expect(sent[0]!.text).toContain('@spammer_x')
    expect(sent[1]!.text).toContain('лимит LLM-расходов')

    // Second cycle immediately must NOT duplicate either message
    await runAdminAlertCycle(
      store,
      api,
      settings,
      status,
      new RuntimeLog(settings),
      new AbortController().signal,
    )
    expect(sent).toHaveLength(2)

    // Next UTC day -> budget resolves and sends resolution notice
    clock.now += 86_400_000
    await runAdminAlertCycle(
      store,
      api,
      settings,
      status,
      new RuntimeLog(settings),
      new AbortController().signal,
    )
    expect(sent).toHaveLength(3)
    expect(sent[2]!.text).toContain('снова в норме')
  })

  test('smart /approve @username, /recent, /status, and approve callback work with strict security', async () => {
    const clock = { now: 1_700_000_000_000 }
    const settings = makeSettings({ reputationThreshold: 0 })
    const store = openStore(settings, clock)
    store.bindBot(777, 'misaka_bot')

    store.ingest(obs(1, { userId: 42, username: 'alice_dev', displayName: 'Alice' }))
    store.ingest(obs(2, { userId: 50, username: 'dup_name' }))
    store.ingest(obs(3, { userId: 51, username: 'dup_name' }))
    const job = store.claimClassification()!
    store.completeClassification(job, {
      verdict: 'spam',
      reason: 'Тестовый спам',
      evidenceMessageIds: [1],
      model: 'gpt-6-luna',
      promptVersion: 'v3',
      usage: { input_tokens: 1000, output_tokens: 50, cached_input_tokens: 0 },
    })

    const sent: Array<{
      chatId: number | string
      text: string
      options?: Record<string, unknown>
    }> = []
    const answers: string[] = []
    const api: Parameters<typeof handleCommand>[3] = {
      sendMessage: async (chatId, text, options) => {
        sent.push({ chatId, text, options })
        return { message_id: sent.length }
      },
      answerCallbackQuery: async (_id, opts) => {
        answers.push(opts?.text ?? '')
        return true
      },
    }
    const telegram: TelegramPort = {
      botId: 777,
      botUsername: 'misaka_bot',
      isAdmin: async () => true,
      ban: async () => undefined,
      unban: async () => undefined,
      membership: async () => ({ restricted: false, isMember: true }),
      delete: async () => undefined,
      notify: async () => 1,
      edit: async () => undefined,
    }

    const run = async (
      id: string,
      chatId: number,
      userId: number,
      text: string,
      callbackId?: string,
    ) => {
      const cmd = { id, chatId, userId, text, ...(callbackId ? { callbackId } : {}) }
      store.enqueueCommand(cmd)
      await handleCommand(cmd, store, telegram, api, settings, new AbortController().signal)
    }

    // Unique observed @username resolves cleanly
    await run('c1', -100, 99, '/approve @alice_dev')
    expect(sent.at(-1)!.text).toContain('42')

    // Ambiguous @username refuses to guess and lists both numeric IDs
    await run('c2', -100, 99, '/approve @dup_name')
    expect(sent.at(-1)!.text).toContain('50, 51')

    // /status renders readable Russian dashboard with model, spend, and chat name
    await run('c3', 99, 99, '/status')
    expect(sent.at(-1)!.text).toContain('openai / gpt-6-luna (low)')
    expect(sent.at(-1)!.text).toContain('Mobile DI')

    // /recent in DM shows spam decision with snippet and chat name
    await run('c4', 99, 99, '/recent')
    expect(sent.at(-1)!.text).toContain('Mobile DI')
    expect(sent.at(-1)!.text).toContain('Тестовый спам')

    // approve callback from DM approves user 50
    await run('c5', 99, 99, 'approve:-100:50', 'cb-1')
    expect(answers.at(-1)).toContain('50')
  })

  test('config loader selects OpenAI gpt-6-luna by default when OPENAI_API_KEY is configured', () => {
    const cfg = loadSettings({
      BOT_TOKEN: '123:test',
      OPENAI_API_KEY: 'sk-test',
      ADMIN_ID: '1',
      TRACKING_CHAT_IDS: '-1001052301161',
    })
    expect(cfg.provider).toBe('openai')
    expect(cfg.model).toBe('gpt-6-luna')
    expect(cfg.reasoningEffort).toBe('low')
    expect(cfg.chatNames?.['-1001052301161']).toBe('Mobile DI')
  })

  test('v1 SQLite database upgrades in-place to v2 without losing existing records', () => {
    const dir = mkdtempSync(join(tmpdir(), 'misaka-v1-'))
    directories.push(dir)
    const dbPath = join(dir, 'v1.sqlite')
    const raw = new Database(dbPath, { create: true })
    raw.exec(`
      CREATE TABLE subjects (
        chatId INTEGER NOT NULL,
        userId INTEGER NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL DEFAULT 0,
        approved TEXT,
        processedSeq INTEGER NOT NULL DEFAULT 0,
        activeDecision TEXT,
        restrictionEpoch INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chatId,userId)
      );
      INSERT INTO subjects(chatId, userId, approved) VALUES(-100, 77, 'explicit');
      PRAGMA user_version=1;
    `)
    raw.close()

    const store = openStore(makeSettings({ dbPath }), { now: Date.now() })
    expect(store.status().approved).toBe(1)
    expect(store.todayUsage().requests).toBe(0)
  })
})
