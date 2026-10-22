import type { StorageContext } from "./context";

/** Reclaim abandoned leases; never replay shadow work as live moderation. */
export function recoverInterruptedWork(context: StorageContext): void {
  const { settings } = context;

  for (const chatId of settings.chatIds) {
    context.run(
      `
  INSERT OR IGNORE INTO chats(chatId)
  VALUES(?)
`,
      chatId,
    );
  }
  const interrupted = context.all<{
    id: string;
    attempt: number;
  }>(`
  SELECT id,attempt
  FROM jobs
  WHERE state='running'
`);
  for (const job of interrupted) {
    context.run(
      `
  UPDATE jobs
  SET state='retry',token='',retryAt=?,error='interrupted classification'
  WHERE id=?
`,
      context.now() +
        (job.attempt >= settings.maxClassificationAttempts ? settings.recoveryIntervalMs : 0),
      job.id,
    );
    context.audit("classification_interrupted", { id: job.id, attempt: job.attempt });
  }
  for (const action of context.all<{
    id: string;
  }>(`
  SELECT id
  FROM actions
  WHERE state IN ('preflight','sending','reconciling')
`)) {
    context.run(
      `
  UPDATE actions
  SET state='unknown',token='',retryAt=?,error='interrupted action; reconcile before retry'
  WHERE id=?
`,
      context.now(),
      action.id,
    );
    context.audit("action_interrupted", { id: action.id });
  }
  context.run(
    `
  UPDATE attempts
  SET finishedAt=?,outcome='interrupted',error='process restarted'
  WHERE finishedAt IS NULL
`,
    context.now(),
  );
  context.run(
    `
  UPDATE commands
  SET state='pending',retryAt=?
  WHERE state='running'
`,
    context.now(),
  );
  for (const chatId of settings.chatIds) {
    context.run(
      `
  INSERT OR IGNORE INTO chat_aliases(aliasId,canonicalId)
  VALUES(?,?)
`,
      chatId,
      chatId,
    );
  }
  if (settings.mode === "live") {
    // Shadow evidence is historical, not a live backlog. Only future evidence may act.
    for (const row of context.all<{
      chatId: number;
      userId: number;
      activeDecision: string | null;
    }>(`
  SELECT s.chatId,s.userId,s.activeDecision
  FROM subjects s
  JOIN actions a ON a.decisionId=s.activeDecision
  AND a.kind='ban'
  WHERE a.mode='shadow'
`)) {
      context.run(
        `
  UPDATE subjects
  SET activeDecision=NULL,generation=generation+1,processedSeq=MAX(processedSeq,?)
  WHERE chatId=?
  AND userId=?
`,
        context.highwater(row.chatId, row.userId),
        row.chatId,
        row.userId,
      );
      context.audit("shadow_generation_closed", { decisionId: row.activeDecision });
    }
  }
}
