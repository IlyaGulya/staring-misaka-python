import type { ActionAttempt, ActionDetail, StoreHealth, Decision } from "../domain";
import { StorageContext } from "./context";
import type { DecisionRecord, ActionRecord } from "./records";
import { decode, ActionPayloadSchema, ClassificationSchema } from "./codecs";

export function getOffset(context: StorageContext): number {
  const offset = context.get<{ value: string }>(`
        SELECT value FROM meta WHERE key = 'offset'
    `);
  return Number(offset?.value ?? 0);
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
  );
  if (!row) {
    return undefined;
  }
  return {
    ...decode(row.payload, ClassificationSchema),
    id: row.id,
    chatId: context.resolveChatId(row.chatId),
    userId: row.userId,
    snapshotId: row.snapshotId,
    createdAt: row.createdAt,
    overriddenAt: row.overriddenAt ?? null,
    status: row.overriddenAt !== null ? "overridden" : row.status,
  };
}

export function decisionsFor(context: StorageContext, userId: number): Decision[] {
  const decisions = context.all<{ id: string }>(
    `
        SELECT id FROM decisions
        WHERE userId = ?
        ORDER BY createdAt DESC, rowid DESC
    `,
    userId,
  );
  return decisions.map((decision) => getDecision(context, decision.id)!);
}

export function decisionForMessage(
  context: StorageContext,
  chatId: number,
  messageId: number,
): Decision | undefined {
  const row = context.get<{
    id: string;
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
  );
  return row ? getDecision(context, row.id) : undefined;
}

export function chatEnabled(context: StorageContext, chatId: number): boolean {
  const chat = context.get<{ enabled: number }>(
    `
        SELECT enabled FROM chats WHERE chatId = ?
    `,
    context.canonicalChatId(chatId),
  );
  return chat?.enabled === 1;
}

export function status(context: StorageContext, chatId?: number): Record<string, number> {
  if (chatId !== undefined) {
    chatId = context.canonicalChatId(chatId);
  }
  const result: Record<string, number> = {};
  const where = chatId === undefined ? "" : " WHERE chatId=?";
  const args = chatId === undefined ? [] : [chatId];
  for (const table of ["subjects", "decisions", "snapshots"]) {
    result[table] = context.get<{
      n: number;
    }>(
      `
  SELECT COUNT(*) AS n FROM ${table}${where}
`,
      ...args,
    )!.n;
  }
  result.messages = context.get<{
    n: number;
  }>(
    `
  SELECT COUNT(*) AS n FROM messages${chatId === undefined ? "" : " WHERE chatId IN (SELECT aliasId FROM chat_aliases WHERE canonicalId=?)"}
`,
    ...args,
  )!.n;
  for (const table of ["jobs", "actions"]) {
    for (const row of context.all<{
      state: string;
      n: number;
    }>(
      `
  SELECT state,COUNT(*) AS n FROM ${table}${where} GROUP BY state
`,
      ...args,
    )) {
      result[`${table}_${row.state}`] = row.n;
    }
  }
  result.approved = context.get<{
    n: number;
  }>(
    `
  SELECT COUNT(*) AS n FROM subjects WHERE approved IS NOT NULL${chatId === undefined ? "" : " AND chatId=?"}
`,
    ...args,
  )!.n;
  result.command_backlog = context.get<{
    n: number;
  }>(
    `
  SELECT COUNT(*) AS n FROM commands WHERE state!='done'${chatId === undefined ? "" : " AND json_extract(payload,'$.chatId') IN (SELECT aliasId FROM chat_aliases WHERE canonicalId=?)"}
`,
    ...args,
  )!.n;
  return result;
}

export function action(context: StorageContext, id: string): ActionDetail | undefined {
  const row = context.get<ActionRecord>(
    `
  SELECT *
  FROM actions
  WHERE id=?
`,
    id,
  );
  if (!row) {
    return undefined;
  }
  const attempts = context.all<ActionAttempt>(
    `
        SELECT attempt, startedAt, finishedAt, outcome, error
        FROM attempts
        WHERE workType = 'action' AND workId = ?
        ORDER BY id
    `,
    id,
  );
  return {
    ...row,
    chatId: context.resolveChatId(row.chatId),
    payload: decode(row.payload, ActionPayloadSchema),
    attempts,
  };
}

export function health(context: StorageContext): StoreHealth {
  const journal = context.get<{ value: string }>(`
        SELECT value FROM meta WHERE key = 'journalCursor'
    `);
  return {
    mode: context.settings.mode,
    offset: getOffset(context),
    journalCursor: Number(journal?.value ?? 0),
    ...status(context),
  };
}
