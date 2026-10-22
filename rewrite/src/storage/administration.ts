import { AppError, type Command } from "../domain";
import { StorageContext } from "./context";
import type { CommandRecord } from "./records";
import { encode, decode, CommandSchema } from "./codecs";
import { overrideDecision } from "./actions";
import { invalidateJobs } from "./subject-lifecycle";
import { chatEnabled } from "./queries";

/** Workflows here run inside the Store-owned SQLite transaction unless documented otherwise. */
export function enqueueCommand(context: StorageContext, command: Command): void {
  context.run(
    `
  INSERT OR IGNORE INTO commands(id,payload,createdAt)
  VALUES(?,?,?)
`,
    command.id,
    encode(command),
    context.now(),
  );
}

export function claimCommand(context: StorageContext): Command | undefined {
  const row = context.get<CommandRecord>(
    `
  SELECT *
  FROM commands
  WHERE state IN ('pending','retry')
  AND retryAt<=?
  ORDER BY createdAt,id
  LIMIT 1
`,
    context.now(),
  );
  if (!row) {
    return undefined;
  }
  context.run(
    `
  UPDATE commands
  SET state='running',attempt=attempt+1
  WHERE id=?
`,
    row.id,
  );
  return decode(row.payload, CommandSchema);
}

export function applyCommand<T>(
  context: StorageContext,
  id: string,
  mutation: () => T,
): T | undefined {
  const command = context.get<{
    applied: number;
  }>(
    `
  SELECT applied
  FROM commands
  WHERE id=?
`,
    id,
  );
  if (!command) {
    throw new AppError({ category: "invalid", message: "Command was not durably enqueued" });
  }
  if (command.applied) {
    return undefined;
  }
  const result = mutation();
  context.run(
    `
  UPDATE commands
  SET applied=1
  WHERE id=?
`,
    id,
  );
  context.audit("command_applied", { id });
  return result;
}

export function finishCommand(context: StorageContext, id: string): void {
  context.run(
    `
  UPDATE commands
  SET state='done',error=NULL
  WHERE id=?
  AND state='running'
`,
    id,
  );
}

export function failCommand(context: StorageContext, id: string, error: string): void {
  const row = context.get<{
    attempt: number;
  }>(
    `
  SELECT attempt
  FROM commands
  WHERE id=?
  AND state='running'
`,
    id,
  );
  if (!row) {
    return;
  }
  context.run(
    `
  UPDATE commands
  SET state='retry',error=?,retryAt=?
  WHERE id=?
`,
    error,
    context.now() + context.delay(row.attempt, context.settings.maxActionAttempts),
    id,
  );
  context.audit("command_failed", { id, error, attempt: row.attempt });
}

export function approve(
  context: StorageContext,
  chatId: number,
  userId: number,
  actorId: number,
): void {
  chatId = context.canonicalChatId(chatId);
  context.enroll(chatId, userId);
  const subject = context.subject(chatId, userId);
  if (subject.activeDecision) {
    overrideDecision(context, subject.activeDecision, actorId);
  }
  invalidateJobs(context, chatId, userId);
  context.run(
    `
  UPDATE subjects
  SET approved='explicit',version=version+1,processedSeq=MAX(processedSeq,?)
  WHERE chatId=?
  AND userId=?
`,
    context.highwater(chatId, userId),
    chatId,
    userId,
  );
  context.audit("approved", { chatId, userId, actorId, origin: "explicit" });
}

export function unapprove(
  context: StorageContext,
  chatId: number,
  userId: number,
  actorId: number,
): void {
  chatId = context.canonicalChatId(chatId);
  context.enroll(chatId, userId);
  invalidateJobs(context, chatId, userId);
  context.run(
    `
  UPDATE subjects
  SET approved=NULL,version=version+1,processedSeq=MAX(processedSeq,?)
  WHERE chatId=?
  AND userId=?
`,
    context.highwater(chatId, userId),
    chatId,
    userId,
  );
  context.audit("unapproved", { chatId, userId, actorId });
}

export function toggleChat(context: StorageContext, chatId: number, enabled?: boolean): boolean {
  chatId = context.canonicalChatId(chatId);
  const next = enabled ?? !chatEnabled(context, chatId);
  context.run(
    `
  INSERT INTO chats(chatId,enabled)
  VALUES(?,?)
  ON CONFLICT(chatId)
  DO UPDATE SET enabled=excluded.enabled
`,
    chatId,
    Number(next),
  );
  context.audit("chat_monitoring", { chatId, enabled: next });
  return next;
}
