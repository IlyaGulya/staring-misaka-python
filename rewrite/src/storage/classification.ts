import { randomUUID } from 'node:crypto'
import { Schema } from 'effect'
import {
  AppError,
  type Classification,
  type ClassificationJob,
  type ContextMessage,
  type Snapshot,
} from '../domain'
import { StorageContext } from './context'
import type {
  SubjectRecord,
  RevisionRecord,
  ClassificationRecord,
  ClassificationSnapshotRecord,
} from './records'
import {
  encode,
  decode,
  ObjectSchema,
  MediaSchema,
  SnapshotSchema,
  ClassificationSchema,
} from './codecs'
import { addAction } from './actions'
import { isDailyBudgetExceeded, recordUsage } from './budget'
import { assessRevisionRisk, isSubstantiveMessage } from './reputation'

const CONTEXT_MAX_MESSAGES = 10
const CONTEXT_MAX_CHARS = 8_000
const CONTEXT_MESSAGE_MAX_CHARS = 1_000
const CONTEXT_WINDOW_MS = 24 * 60 * 60 * 1000
const CONTEXT_SAME_AUTHOR_RESERVE = 3
const BURST_WINDOW_MS = 30_000

interface PriorContextRow {
  seq: number
  messageId: number
  userId: number | null
  text: string
  metadata: string
  stamp: number
}

function senderLabel(metadata: Record<string, unknown>, userId: number | null): string | undefined {
  const sender = metadata.sender
  if (sender && typeof sender === 'object') {
    const username =
      'username' in sender && typeof sender.username === 'string' && sender.username
        ? `@${sender.username}`
        : ''
    const name = [
      'firstName' in sender ? sender.firstName : undefined,
      'lastName' in sender ? sender.lastName : undefined,
    ]
      .filter((part): part is string => typeof part === 'string' && part.length > 0)
      .join(' ')
    if (name && username) return `${name} (${username})`
    if (name) return name
    if (username) return username
  }
  return userId !== null ? `user:${userId}` : undefined
}

/** Collect strictly causal, past-only chat context (reply parent + same-author + recent chat). */
function buildCausalContext(
  context: StorageContext,
  canonicalChatId: number,
  subjectUserId: number,
  firstTargetSeq: number,
  targetMessageIds: ReadonlySet<number>,
  replyParentIds: ReadonlySet<number>,
  targetMinStamp: number,
): { readonly context: ContextMessage[]; readonly truncated: boolean } {
  const recentWindowStart = Math.max(0, targetMinStamp - CONTEXT_WINDOW_MS)
  const candidates = context.all<PriorContextRow>(
    `
    SELECT r.seq, r.messageId, r.userId, r.text, r.metadata, m.stamp
    FROM revisions r
    JOIN messages m USING(chatId, messageId, revision)
    WHERE r.chatId IN (SELECT aliasId FROM chat_aliases WHERE canonicalId = ?)
      AND r.seq < ?
      AND r.actorKind != 'unknown'
      AND (m.stamp >= ? OR r.messageId IN (SELECT value FROM json_each(?)))
    ORDER BY r.seq DESC
    LIMIT 64
  `,
    canonicalChatId,
    firstTargetSeq,
    recentWindowStart,
    JSON.stringify([...replyParentIds]),
  )

  // Exclude target messages themselves and keep only the latest revision per prior messageId.
  const seen = new Set<number>(targetMessageIds)
  const unique: PriorContextRow[] = []
  for (const row of candidates) {
    if (seen.has(row.messageId)) continue
    seen.add(row.messageId)
    unique.push(row)
  }
  if (unique.length === 0) return { context: [], truncated: false }

  const replyRows: PriorContextRow[] = []
  const sameAuthorRows: PriorContextRow[] = []
  const otherRows: PriorContextRow[] = []
  for (const row of unique) {
    if (replyParentIds.has(row.messageId)) {
      replyRows.push(row)
    } else if (row.userId === subjectUserId) {
      sameAuthorRows.push(row)
    } else {
      otherRows.push(row)
    }
  }

  const picked: PriorContextRow[] = []
  const pickedIds = new Set<number>()
  const add = (row: PriorContextRow): void => {
    if (picked.length >= CONTEXT_MAX_MESSAGES || pickedIds.has(row.messageId)) return
    picked.push(row)
    pickedIds.add(row.messageId)
  }

  for (const row of replyRows) add(row)
  for (const row of sameAuthorRows.slice(0, CONTEXT_SAME_AUTHOR_RESERVE)) add(row)
  // Fill remaining slots with newest surrounding messages (both same-author and other chat users).
  for (const row of unique) add(row)

  let truncated = unique.length > picked.length
  // Enforce char budget, reserving reply parents first.
  const budgeted: PriorContextRow[] = []
  let remainingChars = CONTEXT_MAX_CHARS
  const prioritized = [
    ...picked.filter((row) => replyParentIds.has(row.messageId)),
    ...picked.filter((row) => !replyParentIds.has(row.messageId)),
  ]
  for (const row of prioritized) {
    const clippedLength = Math.min(row.text.length, CONTEXT_MESSAGE_MAX_CHARS)
    if (clippedLength > remainingChars) {
      truncated = true
      continue
    }
    remainingChars -= clippedLength
    budgeted.push(row)
  }

  budgeted.sort((a, b) => a.seq - b.seq)
  const messages: ContextMessage[] = []
  for (const row of budgeted) {
    let parsedMeta: Record<string, unknown> = {}
    try {
      parsedMeta = decode(row.metadata, ObjectSchema)
    } catch {
      parsedMeta = {}
    }
    const text = row.text.slice(0, CONTEXT_MESSAGE_MAX_CHARS)
    if (text.length < row.text.length) truncated = true
    const sender = senderLabel(parsedMeta, row.userId)
    messages.push({
      messageId: row.messageId,
      ...(row.userId === null ? {} : { userId: row.userId }),
      sameAuthor: row.userId === subjectUserId,
      isReplyParent: replyParentIds.has(row.messageId),
      ...(sender ? { sender } : {}),
      occurredAt: row.stamp,
      text,
    })
  }
  return { context: messages, truncated }
}

export function claimClassification(context: StorageContext): ClassificationJob | undefined {
  if (isDailyBudgetExceeded(context)) {
    return undefined
  }

  let row = context.get<ClassificationSnapshotRecord>(
    `
  SELECT j.*,s.payload
  FROM jobs j
  JOIN snapshots s ON s.id=j.snapshotId
  JOIN chats c ON c.chatId=j.chatId
  WHERE j.state IN ('pending','retry','blocked')
  AND j.retryAt<=?
  AND c.enabled=1
  ORDER BY j.retryAt,j.createdAt
  LIMIT 1
`,
    context.now(),
  )

  if (!row) {
    const threshold = context.settings.reputationThreshold ?? 0
    const excluded = new Set<string>()

    while (!row) {
      const subject = context.get<SubjectRecord>(
        `
            SELECT u.*
            FROM subjects u
            JOIN chats c ON c.chatId = u.chatId
            WHERE u.approved IS NULL AND u.activeDecision IS NULL AND c.enabled = 1
              AND NOT EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.chatId = u.chatId AND j.userId = u.userId
                  AND j.state IN ('pending', 'running', 'retry', 'blocked')
              )
              AND EXISTS (
                SELECT 1
                FROM revisions r
                JOIN messages m USING(chatId, messageId, revision)
                WHERE r.chatId = COALESCE(
                    (SELECT destinationId FROM chat_routes WHERE canonicalId = u.chatId),
                    u.chatId
                  )
                  AND r.userId = u.userId AND r.eligible = 1 AND r.seq > u.processedSeq
              )
            ORDER BY u.lastCleanAt ASC, u.rowid ASC
            LIMIT 1
        `,
      )
      if (!subject) {
        return undefined
      }
      const subjectKey = `${subject.chatId}:${subject.userId}`
      if (excluded.has(subjectKey)) {
        return undefined
      }
      excluded.add(subjectKey)

      const resolvedChatId = context.resolveChatId(subject.chatId)
      const rows = context.all<RevisionRecord>(
        `
  SELECT r.*
  FROM revisions r
  JOIN messages m USING(chatId,messageId,revision)
  WHERE r.chatId=?
  AND r.userId=?
  AND r.eligible=1
  AND r.seq>?
  ORDER BY r.seq
  LIMIT 20
`,
        resolvedChatId,
        subject.userId,
        subject.processedSeq,
      )
      const firstRow = rows[0]
      if (!firstRow) {
        continue
      }
      const mode = firstRow.mode
      const boundary = rows.findIndex((revision) => revision.mode !== mode)
      if (boundary >= 0) {
        rows.length = boundary
      }
      const lastRow = rows.at(-1)
      if (!lastRow) {
        continue
      }

      // Soft reputation gate: established regulars skip LLM only on plain, unedited, non-burst text.
      if (threshold > 0 && subject.cleanCount >= threshold) {
        const latestStamp =
          context.get<{ stamp: number }>(
            `
            SELECT COALESCE(MAX(stamp), 0) AS stamp
            FROM messages
            WHERE chatId = ? AND userId = ?
          `,
            resolvedChatId,
            subject.userId,
          )?.stamp ?? context.now()
        const recentBurstCount =
          context.get<{ n: number }>(
            `
            SELECT COUNT(*) AS n
            FROM messages
            WHERE chatId = ? AND userId = ? AND stamp >= ?
          `,
            resolvedChatId,
            subject.userId,
            latestStamp - BURST_WINDOW_MS,
          )?.n ?? rows.length

        const riskyRevision = rows.some((revision) => {
          const media = decode(revision.media, MediaSchema)
          const metadata = decode(revision.metadata, ObjectSchema)
          return assessRevisionRisk(
            { revision: revision.revision, text: revision.text, media, metadata },
            recentBurstCount,
          ).risky
        })

        if (!riskyRevision) {
          const highwater = lastRow.seq
          context.run(
            `
            UPDATE subjects
            SET processedSeq = MAX(processedSeq, ?)
            WHERE chatId = ? AND userId = ?
          `,
            highwater,
            subject.chatId,
            subject.userId,
          )
          context.audit('classification_skipped_reputation', {
            chatId: subject.chatId,
            userId: subject.userId,
            cleanCount: subject.cleanCount,
            messageIds: rows.map((r) => r.messageId),
          })
          continue
        }
      }

      let characters = 16_000
      let mediaLeft = 4
      const targetMessageIds = new Set<number>()
      const replyParentIds = new Set<number>()
      const messages = rows.map((revision) => {
        targetMessageIds.add(revision.messageId)
        const text = revision.text.slice(0, Math.min(4_000, characters))
        characters -= text.length
        const originalMedia = decode(revision.media, MediaSchema)
        const media = originalMedia.slice(0, mediaLeft)
        mediaLeft -= media.length
        const rawMeta = decode(revision.metadata, ObjectSchema)
        if (
          typeof rawMeta.replyToMessageId === 'number' &&
          Number.isSafeInteger(rawMeta.replyToMessageId) &&
          rawMeta.replyToMessageId > 0
        ) {
          replyParentIds.add(rawMeta.replyToMessageId)
        }
        const metadata = boundedMetadata(rawMeta)
        return {
          messageId: revision.messageId,
          revision: revision.revision,
          text,
          media,
          metadata: {
            ...metadata,
            originChatId: revision.chatId,
            evidenceTruncated:
              Boolean(metadata.evidenceTruncated) ||
              text.length < revision.text.length ||
              media.length < originalMedia.length,
          },
        }
      })

      const targetMinStamp =
        context.get<{ stamp: number }>(
          `
          SELECT COALESCE(MIN(stamp), ?) AS stamp
          FROM messages
          WHERE chatId = ? AND messageId IN (SELECT value FROM json_each(?))
        `,
          context.now(),
          resolvedChatId,
          JSON.stringify([...targetMessageIds]),
        )?.stamp ?? context.now()

      const causal = buildCausalContext(
        context,
        subject.chatId,
        subject.userId,
        firstRow.seq,
        targetMessageIds,
        replyParentIds,
        targetMinStamp,
      )

      const snapshot: Snapshot = {
        id: randomUUID(),
        chatId: subject.chatId,
        userId: subject.userId,
        generation: subject.generation,
        version: subject.version,
        messages,
        ...(causal.context.length > 0 ? { context: causal.context } : {}),
        ...(causal.truncated ? { contextTruncated: true } : {}),
      }
      context.run(
        `
  INSERT INTO snapshots(id,chatId,userId,highwater,payload,createdAt)
  VALUES(?,?,?,?,?,?)
`,
        snapshot.id,
        subject.chatId,
        subject.userId,
        lastRow.seq,
        encode(snapshot),
        context.now(),
      )
      const id = randomUUID()
      context.run(
        `
  INSERT INTO jobs(id,snapshotId,chatId,userId,state,mode,createdAt)
  VALUES(?,?,?,?,'pending',?,?)
`,
        id,
        snapshot.id,
        subject.chatId,
        subject.userId,
        mode,
        context.now(),
      )
      row = context.get<ClassificationSnapshotRecord>(
        `
  SELECT j.*,s.payload
  FROM jobs j
  JOIN snapshots s ON s.id=j.snapshotId
  WHERE j.id=?
`,
        id,
      )
      context.audit('classification_scheduled', { id, snapshotId: snapshot.id })
    }
  }

  if (!row) {
    return undefined
  }

  const token = randomUUID()
  context.run(
    `
  UPDATE jobs
  SET state='running',attempt=attempt+1,token=?
  WHERE id=?
`,
    token,
    row.id,
  )
  context.startAttempt('classification', row.id, row.attempt + 1, token)
  return {
    id: row.id,
    token,
    attempt: row.attempt + 1,
    snapshot: decode(row.payload, SnapshotSchema),
  }
}

export function completeClassification(
  context: StorageContext,
  job: ClassificationJob,
  result: Classification,
): boolean {
  result = Schema.decodeUnknownSync(ClassificationSchema)(result)
  const current = context.get<ClassificationRecord>(
    `
  SELECT *
  FROM jobs
  WHERE id=?
  AND token=?
  AND state='running'
`,
    job.id,
    job.token,
  )
  if (!current) {
    return false
  }
  const subject = context.subject(job.snapshot.chatId, job.snapshot.userId)
  if (
    subject.version !== job.snapshot.version ||
    subject.generation !== job.snapshot.generation ||
    subject.approved ||
    subject.activeDecision
  ) {
    context.run(
      `
  UPDATE jobs
  SET state='cancelled',token=''
  WHERE id=?
`,
      job.id,
    )
    context.finishAttempt('classification', job.id, job.token, 'stale')
    return false
  }
  const validIds = new Set(job.snapshot.messages.map((message) => message.messageId))
  if (
    !['spam', 'clean', 'uncertain'].includes(result.verdict) ||
    !result.reason ||
    !result.model ||
    !result.promptVersion ||
    result.evidenceMessageIds.some((id) => !validIds.has(id))
  ) {
    throw new AppError({
      category: 'invalid',
      message: 'Classifier returned invalid evidence or verdict',
    })
  }

  // Record token spend whenever an external model call completed.
  if (result.usage && Object.keys(result.usage).length > 0) {
    recordUsage(context, subject.chatId, result.usage)
  }

  const id = randomUUID()
  context.run(
    `
  INSERT INTO decisions(id,chatId,userId,snapshotId,payload,createdAt,status)
  VALUES(?,?,?,?,?,?,?)
`,
    id,
    subject.chatId,
    subject.userId,
    job.snapshot.id,
    encode(result),
    context.now(),
    result.verdict,
  )
  const highwater =
    context.get<{
      highwater: number
    }>(
      `
  SELECT highwater
  FROM snapshots
  WHERE id=?
`,
      job.snapshot.id,
    )?.highwater ?? 0

  const minChars = context.settings.reputationMinChars ?? 30
  const earnedReputation =
    result.verdict === 'clean' &&
    job.snapshot.messages.some((message) => isSubstantiveMessage(message.text, minChars))

  context.run(
    `
  UPDATE subjects
  SET
    processedSeq = MAX(processedSeq, ?),
    cleanCount = cleanCount + ?,
    lastCleanAt = CASE WHEN ? > 0 THEN ? ELSE lastCleanAt END
  WHERE chatId=?
  AND userId=?
`,
    highwater,
    earnedReputation ? 1 : 0,
    earnedReputation ? 1 : 0,
    context.now(),
    subject.chatId,
    subject.userId,
  )
  context.run(
    `
  UPDATE jobs
  SET state='done',token=''
  WHERE id=?
`,
    job.id,
  )
  context.finishAttempt('classification', job.id, job.token, 'confirmed')
  if (result.verdict === 'spam') {
    const mode = context.settings.mode === 'shadow' ? 'shadow' : current.mode
    if (mode === 'live' || context.settings.mode === 'shadow') {
      context.run(
        `
  UPDATE subjects
  SET activeDecision=?
  WHERE chatId=?
  AND userId=?
`,
        id,
        subject.chatId,
        subject.userId,
      )
    }
    addAction(context, {
      kind: 'ban',
      decisionId: id,
      subject: subject,
      payload: {},
      retryAt: 0,
      originatingMode: mode,
    })

    // Scoped cleanup: delete cited evidence messages plus messages within the recent episode window.
    // Never wipe multi-year history if an established member hits a false positive.
    const cleanupWindowMs = context.settings.banCleanupWindowMs ?? 86_400_000
    const latestEvidenceStamp =
      context.get<{ stamp: number }>(
        `
        SELECT COALESCE(MAX(stamp), 0) AS stamp
        FROM messages
        WHERE chatId IN (SELECT aliasId FROM chat_aliases WHERE canonicalId=?)
          AND userId=?
          AND messageId IN (SELECT value FROM json_each(?))
      `,
        subject.chatId,
        subject.userId,
        JSON.stringify([...validIds]),
      )?.stamp ?? 0
    const episodeAnchor = latestEvidenceStamp > 0 ? latestEvidenceStamp : context.now()
    const cleanupCutoff = episodeAnchor - cleanupWindowMs

    for (const message of context.all<{
      chatId: number
      messageId: number
    }>(
      `
  SELECT chatId,messageId
  FROM messages
  WHERE chatId IN (SELECT aliasId
  FROM chat_aliases
  WHERE canonicalId=?)
  AND userId=?
  AND deleted=0
  AND (
    stamp >= ?
    OR messageId IN (SELECT value FROM json_each(?))
  )
`,
      subject.chatId,
      subject.userId,
      cleanupCutoff,
      JSON.stringify(result.evidenceMessageIds),
    )) {
      addAction(context, {
        kind: 'delete',
        decisionId: id,
        subject: subject,
        messageId: message.messageId ?? undefined,
        payload: { reason: 'spam_cleanup', messageChatId: message.chatId },
      })
    }
    addAction(context, {
      kind: 'notify',
      decisionId: id,
      subject: subject,
      payload: { reason: result.reason.slice(0, 500) },
    })
    if (context.settings.adminBanNotifications !== false) {
      context.run(
        `
  INSERT OR IGNORE INTO admin_notifications(decisionId, state, createdAt)
  VALUES(?, 'pending', ?)
`,
        id,
        context.now(),
      )
    }
  }
  context.audit('decision', { id, snapshotId: job.snapshot.id, ...result })
  return true
}

export function failClassification(
  context: StorageContext,
  job: ClassificationJob,
  error: AppError,
): void {
  const state = error.category === 'permanent' || error.category === 'invalid' ? 'blocked' : 'retry'
  const delay =
    state === 'blocked'
      ? context.settings.recoveryIntervalMs
      : context.delay(job.attempt, context.settings.maxClassificationAttempts)
  if (
    context.run(
      `
  UPDATE jobs
  SET state=?,retryAt=?,error=?,token=''
  WHERE id=?
  AND token=?
  AND state='running'
`,
      state,
      context.now() + Math.max(delay, error.retryAfterMs ?? 0),
      error.message,
      job.id,
      job.token,
    )
  ) {
    context.finishAttempt('classification', job.id, job.token, state, error.message)
    context.audit('classification_failed', {
      id: job.id,
      attempt: job.attempt,
      state,
      error: error.message,
    })
  }
}

/** Bound classifier context without changing the archived full-fidelity observation. */
function boundedMetadata(metadata: Record<string, unknown>): Record<string, unknown> {
  let remaining = 8000
  function bound(value: unknown, depth: number): unknown {
    if (remaining <= 0 || depth > 5) {
      return null
    }
    if (typeof value === 'string') {
      const text = value.slice(0, Math.min(remaining, 2000))
      remaining -= text.length
      return text
    }
    if (Array.isArray(value)) {
      return value.slice(0, 32).map((item) => bound(item, depth + 1))
    }
    if (value && typeof value === 'object') {
      const result: Record<string, unknown> = {}
      for (const [key, child] of Object.entries(value).slice(0, 32)) {
        remaining -= key.length
        result[key] = bound(child, depth + 1)
      }
      return result
    }
    return value
  }
  const result: Record<string, unknown> = Schema.decodeUnknownSync(ObjectSchema)(bound(metadata, 0))
  if (encode(result) !== encode(metadata)) {
    result.evidenceTruncated = true
  }
  return result
}
