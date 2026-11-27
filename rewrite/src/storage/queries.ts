import type {
  ActionAttempt,
  ActionDetail,
  Decision,
  Mode,
  RecentDecision,
  StoreHealth,
} from '../domain'
import { StorageContext } from './context'
import type { DecisionRecord, ActionRecord } from './records'
import { decode, ActionPayloadSchema, ClassificationSchema, SnapshotSchema } from './codecs'

export function getOffset(context: StorageContext): number {
  const offset = context.get<{ value: string }>(`
        SELECT value FROM meta WHERE key = 'offset'
    `)
  return Number(offset?.value ?? 0)
}

export function getDecision(context: StorageContext, id: string): Decision | undefined {
  const row = context.get<DecisionRecord>(
    `
  SELECT d.*,o.createdAt AS overriddenAt
  FROM decisions d
  LEFT JOIN overrides o ON o.decisionId=d.id
  WHERE d.id=?
`,
    id,
  )
  if (!row) {
    return undefined
  }
  return {
    ...decode(row.payload, ClassificationSchema),
    id: row.id,
    chatId: context.resolveChatId(row.chatId),
    userId: row.userId,
    snapshotId: row.snapshotId,
    createdAt: row.createdAt,
    overriddenAt: row.overriddenAt ?? null,
    status: row.overriddenAt !== null ? 'overridden' : row.status,
  }
}

export function decisionsFor(context: StorageContext, userId: number): Decision[] {
  const decisions = context.all<{ id: string }>(
    `
        SELECT id FROM decisions
        WHERE userId = ?
        ORDER BY createdAt DESC, rowid DESC
    `,
    userId,
  )
  return decisions.map((decision) => getDecision(context, decision.id)!)
}

export function recentDecisions(
  context: StorageContext,
  limit = 10,
  chatId?: number,
  verdicts: ReadonlyArray<string> = ['spam', 'uncertain'],
): RecentDecision[] {
  const canonical = chatId === undefined ? undefined : context.canonicalChatId(chatId)
  const rows = context.all<
    DecisionRecord & {
      username: string | null
      displayName: string | null
      snapshotPayload: string | null
      actionMode: Mode | null
    }
  >(
    `
    SELECT
      d.*,
      o.createdAt AS overriddenAt,
      u.username AS username,
      u.displayName AS displayName,
      s.payload AS snapshotPayload,
      (SELECT a.mode FROM actions a WHERE a.decisionId = d.id AND a.kind = 'ban' LIMIT 1) AS actionMode
    FROM decisions d
    LEFT JOIN overrides o ON o.decisionId = d.id
    LEFT JOIN subjects u ON u.chatId = d.chatId AND u.userId = d.userId
    LEFT JOIN snapshots s ON s.id = d.snapshotId
    WHERE d.status != 'legacy'
      ${canonical === undefined ? '' : 'AND d.chatId = ?'}
      AND json_extract(d.payload, '$.verdict') IN (SELECT value FROM json_each(?))
    ORDER BY d.createdAt DESC, d.rowid DESC
    LIMIT ?
  `,
    ...(canonical === undefined
      ? [JSON.stringify(verdicts), Math.max(1, Math.min(25, limit))]
      : [canonical, JSON.stringify(verdicts), Math.max(1, Math.min(25, limit))]),
  )

  return rows.map((row) => {
    const classification = decode(row.payload, ClassificationSchema)
    let snippet = ''
    if (row.snapshotPayload) {
      try {
        const snapshot = decode(row.snapshotPayload, SnapshotSchema)
        snippet = snapshot.messages
          .map((m) => m.text.trim())
          .filter(Boolean)
          .join(' / ')
          .slice(0, 140)
      } catch {
        snippet = ''
      }
    }
    return {
      ...classification,
      id: row.id,
      chatId: context.resolveChatId(row.chatId),
      userId: row.userId,
      snapshotId: row.snapshotId,
      createdAt: row.createdAt,
      overriddenAt: row.overriddenAt ?? null,
      status: row.overriddenAt !== null ? 'overridden' : row.status,
      username: row.username ?? null,
      displayName: row.displayName ?? null,
      mode: row.actionMode ?? context.settings.mode,
      snippet,
    }
  })
}

export function resolveUsername(
  context: StorageContext,
  chatId: number,
  rawUsername: string,
): number[] {
  const normalized = rawUsername.trim().replace(/^@/, '').toLowerCase()
  if (!normalized || !/^[a-z0-9_]{3,64}$/.test(normalized)) return []
  const canonical = context.canonicalChatId(chatId)
  return context
    .all<{ userId: number }>(
      `
      SELECT userId
      FROM subjects
      WHERE chatId = ? AND username = ?
      ORDER BY userId ASC
      LIMIT 10
    `,
      canonical,
      normalized,
    )
    .map((row) => row.userId)
}

export function decisionForMessage(
  context: StorageContext,
  chatId: number,
  messageId: number,
): Decision | undefined {
  const row = context.get<{
    id: string
  }>(
    `
  SELECT d.id
  FROM decisions d
  JOIN snapshots s ON s.id=d.snapshotId, json_each(s.payload,'$.messages') m
  WHERE d.chatId=?
  AND json_extract(m.value,'$.messageId')=?
  AND COALESCE(json_extract(m.value,'$.metadata.originChatId'),d.chatId)=?
  ORDER BY d.createdAt DESC,d.rowid DESC
  LIMIT 1
`,
    context.canonicalChatId(chatId),
    messageId,
    chatId,
  )
  return row ? getDecision(context, row.id) : undefined
}

export function chatEnabled(context: StorageContext, chatId: number): boolean {
  const chat = context.get<{ enabled: number }>(
    `
        SELECT enabled FROM chats WHERE chatId = ?
    `,
    context.canonicalChatId(chatId),
  )
  return chat?.enabled === 1
}

export function chatSummaries(context: StorageContext): Array<{
  readonly chatId: number
  readonly name: string
  readonly enabled: boolean
  readonly approved: number
  readonly regulars: number
}> {
  const threshold = context.settings.reputationThreshold ?? 5
  return context.settings.chatIds.map((chatId) => {
    const canonical = context.canonicalChatId(chatId)
    const resolved = context.resolveChatId(canonical)
    const name =
      context.settings.chatNames?.[String(resolved)] ??
      context.settings.chatNames?.[String(chatId)] ??
      `Чат ${resolved}`
    const enabled = chatEnabled(context, canonical)
    const approved =
      context.get<{ n: number }>(
        `SELECT COUNT(*) AS n FROM subjects WHERE chatId = ? AND approved IS NOT NULL`,
        canonical,
      )?.n ?? 0
    const regulars =
      threshold > 0
        ? (context.get<{ n: number }>(
            `SELECT COUNT(*) AS n FROM subjects WHERE chatId = ? AND approved IS NULL AND cleanCount >= ?`,
            canonical,
            threshold,
          )?.n ?? 0)
        : 0
    return { chatId: resolved, name, enabled, approved, regulars }
  })
}

export function blockedAuthJobs(context: StorageContext): number {
  return (
    context.get<{ n: number }>(
      `
      SELECT COUNT(*) AS n
      FROM jobs
      WHERE state = 'blocked'
        AND (error LIKE '%401%' OR error LIKE '%403%' OR error LIKE '%API key%')
    `,
    )?.n ?? 0
  )
}

export function needsAttentionActions(context: StorageContext): Array<{
  readonly id: string
  readonly chatId: number
  readonly userId: number
  readonly error: string | null
}> {
  return context.all<{ id: string; chatId: number; userId: number; error: string | null }>(
    `
    SELECT id, chatId, userId, error
    FROM actions
    WHERE state = 'needs_attention'
    ORDER BY createdAt DESC
    LIMIT 20
  `,
  )
}

export function status(context: StorageContext, chatId?: number): Record<string, number> {
  if (chatId !== undefined) {
    chatId = context.canonicalChatId(chatId)
  }
  const result: Record<string, number> = {}
  const where = chatId === undefined ? '' : ' WHERE chatId=?'
  const args = chatId === undefined ? [] : [chatId]
  for (const table of ['subjects', 'decisions', 'snapshots']) {
    result[table] = context.get<{
      n: number
    }>(
      `
  SELECT COUNT(*) AS n FROM ${table}${where}
`,
      ...args,
    )!.n
  }
  result.messages = context.get<{
    n: number
  }>(
    `
  SELECT COUNT(*) AS n FROM messages${chatId === undefined ? '' : ' WHERE chatId IN (SELECT aliasId FROM chat_aliases WHERE canonicalId=?)'}
`,
    ...args,
  )!.n
  for (const table of ['jobs', 'actions']) {
    for (const row of context.all<{
      state: string
      n: number
    }>(
      `
  SELECT state,COUNT(*) AS n FROM ${table}${where} GROUP BY state
`,
      ...args,
    )) {
      result[`${table}_${row.state}`] = row.n
    }
  }
  result.approved = context.get<{
    n: number
  }>(
    `
  SELECT COUNT(*) AS n FROM subjects WHERE approved IS NOT NULL${chatId === undefined ? '' : ' AND chatId=?'}
`,
    ...args,
  )!.n
  result.command_backlog = context.get<{
    n: number
  }>(
    `
  SELECT COUNT(*) AS n FROM commands WHERE state!='done'${chatId === undefined ? '' : " AND json_extract(payload,'$.chatId') IN (SELECT aliasId FROM chat_aliases WHERE canonicalId=?)"}
`,
    ...args,
  )!.n
  return result
}

export function action(context: StorageContext, id: string): ActionDetail | undefined {
  const row = context.get<ActionRecord>(
    `
  SELECT *
  FROM actions
  WHERE id=?
`,
    id,
  )
  if (!row) {
    return undefined
  }
  const attempts = context.all<ActionAttempt>(
    `
        SELECT attempt, startedAt, finishedAt, outcome, error
        FROM attempts
        WHERE workType = 'action' AND workId = ?
        ORDER BY id
    `,
    id,
  )
  return {
    ...row,
    chatId: context.resolveChatId(row.chatId),
    payload: decode(row.payload, ActionPayloadSchema),
    attempts,
  }
}

export function health(context: StorageContext): StoreHealth {
  const journal = context.get<{ value: string }>(`
        SELECT value FROM meta WHERE key = 'journalCursor'
    `)
  return {
    mode: context.settings.mode,
    offset: getOffset(context),
    journalCursor: Number(journal?.value ?? 0),
    ...status(context),
  }
}
