import type { AdminBanNotice, IncidentAlert, Mode } from '../domain.ts'
import { ClassificationSchema, SnapshotSchema, decode } from './codecs.ts'
import type { StorageContext } from './context.ts'
import type { AdminNotificationRecord } from './records.ts'

/** Deduplicated incident transitions: notify once on open, once on resolve (or every 6h while still open). */
export function recordIncidentState(
  context: StorageContext,
  key: string,
  active: boolean,
  details: string,
  renotifyIntervalMs = 6 * 60 * 60 * 1000,
): { readonly notify: 'opened' | 'resolved' | 'none'; readonly alert?: IncidentAlert } {
  const now = context.now()
  const existing = context.get<IncidentAlert>(
    `SELECT key, state, openedAt, lastNotifiedAt, details FROM incident_alerts WHERE key = ?`,
    key,
  )
  if (active) {
    if (!existing || existing.state === 'resolved') {
      const alert: IncidentAlert = {
        key,
        state: 'open',
        openedAt: now,
        lastNotifiedAt: now,
        details,
      }
      context.run(
        `
        INSERT INTO incident_alerts(key, state, openedAt, lastNotifiedAt, details)
        VALUES(?, 'open', ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          state = 'open',
          openedAt = excluded.openedAt,
          lastNotifiedAt = excluded.lastNotifiedAt,
          details = excluded.details
      `,
        key,
        now,
        now,
        details,
      )
      context.audit('incident_opened', { key, details })
      return { notify: 'opened', alert }
    }
    if (now - existing.lastNotifiedAt >= renotifyIntervalMs) {
      context.run(
        `UPDATE incident_alerts SET lastNotifiedAt = ?, details = ? WHERE key = ?`,
        now,
        details,
        key,
      )
      return {
        notify: 'opened',
        alert: { ...existing, lastNotifiedAt: now, details },
      }
    }
    return { notify: 'none' }
  }

  if (existing && existing.state === 'open') {
    context.run(
      `UPDATE incident_alerts SET state = 'resolved', lastNotifiedAt = ?, details = ? WHERE key = ?`,
      now,
      details,
      key,
    )
    context.audit('incident_resolved', { key, details })
    return {
      notify: 'resolved',
      alert: { ...existing, state: 'resolved', lastNotifiedAt: now, details },
    }
  }
  return { notify: 'none' }
}

export function listOpenIncidents(context: StorageContext): IncidentAlert[] {
  return context.all<IncidentAlert>(
    `SELECT key, state, openedAt, lastNotifiedAt, details FROM incident_alerts WHERE state = 'open' ORDER BY openedAt DESC`,
  )
}

export function claimAdminBanNotice(context: StorageContext): AdminBanNotice | undefined {
  const row = context.get<AdminNotificationRecord>(
    `
    SELECT * FROM admin_notifications
    WHERE state IN ('pending', 'retry') AND retryAt <= ?
    ORDER BY createdAt ASC
    LIMIT 1
  `,
    context.now(),
  )
  if (!row) return undefined

  const decision = context.get<{
    id: string
    chatId: number
    userId: number
    snapshotId: string
    payload: string
    createdAt: number
  }>(
    `SELECT id, chatId, userId, snapshotId, payload, createdAt FROM decisions WHERE id = ?`,
    row.decisionId,
  )
  if (!decision) {
    context.run(
      `UPDATE admin_notifications SET state = 'failed', error = 'decision_missing' WHERE decisionId = ?`,
      row.decisionId,
    )
    return undefined
  }

  const parsed = decode(decision.payload, ClassificationSchema)
  const subject = context.get<{ username: string | null; displayName: string | null }>(
    `SELECT username, displayName FROM subjects WHERE chatId = ? AND userId = ?`,
    decision.chatId,
    decision.userId,
  )
  const snapshotRow = context.get<{ payload: string }>(
    `SELECT payload FROM snapshots WHERE id = ?`,
    decision.snapshotId,
  )
  const banAction = context.get<{ mode: Mode }>(
    `SELECT mode FROM actions WHERE decisionId = ? AND kind = 'ban' LIMIT 1`,
    decision.id,
  )
  let snippet = ''
  if (snapshotRow) {
    try {
      const snapshot = decode(snapshotRow.payload, SnapshotSchema)
      snippet = snapshot.messages
        .map((m) => m.text.trim())
        .filter(Boolean)
        .join(' / ')
        .slice(0, 240)
    } catch {
      snippet = ''
    }
  }
  return {
    decisionId: decision.id,
    chatId: context.resolveChatId(decision.chatId),
    userId: decision.userId,
    username: subject?.username ?? null,
    displayName: subject?.displayName ?? null,
    reason: parsed.reason,
    model: parsed.model,
    mode: banAction?.mode ?? context.settings.mode,
    snippet,
    createdAt: decision.createdAt,
  }
}

export function completeAdminBanNotice(
  context: StorageContext,
  decisionId: string,
  sentMessageId?: number,
): void {
  context.run(
    `UPDATE admin_notifications SET state = 'sent', sentMessageId = ?, error = NULL WHERE decisionId = ?`,
    sentMessageId ?? null,
    decisionId,
  )
}

export function failAdminBanNotice(
  context: StorageContext,
  decisionId: string,
  error: string,
  permanent: boolean,
): void {
  const row = context.get<{ attempt: number }>(
    `SELECT attempt FROM admin_notifications WHERE decisionId = ?`,
    decisionId,
  )
  const nextAttempt = (row?.attempt ?? 0) + 1
  const terminal = permanent || nextAttempt >= 5
  context.run(
    `UPDATE admin_notifications SET state = ?, attempt = ?, retryAt = ?, error = ? WHERE decisionId = ?`,
    terminal ? 'failed' : 'retry',
    nextAttempt,
    context.now() + context.delay(nextAttempt, 5),
    error.slice(0, 300),
    decisionId,
  )
}
