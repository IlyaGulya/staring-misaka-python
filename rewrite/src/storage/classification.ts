import { randomUUID } from "node:crypto";
import { Schema } from "effect";
import { AppError, type Classification, type ClassificationJob, type Snapshot } from "../domain";
import { StorageContext } from "./context";
import type {
  SubjectRecord,
  RevisionRecord,
  ClassificationRecord,
  ClassificationSnapshotRecord,
} from "./records";
import {
  encode,
  decode,
  ObjectSchema,
  MediaSchema,
  SnapshotSchema,
  ClassificationSchema,
} from "./codecs";
import { addAction } from "./actions";

export function claimClassification(context: StorageContext): ClassificationJob | undefined {
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
  );
  if (!row) {
    const subject = context.get<SubjectRecord>(`
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
            ORDER BY u.rowid
            LIMIT 1
        `);
    if (!subject) {
      return undefined;
    }
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
      context.resolveChatId(subject.chatId),
      subject.userId,
      subject.processedSeq,
    );
    const mode = rows[0]!.mode;
    const boundary = rows.findIndex((revision) => revision.mode !== mode);
    if (boundary >= 0) {
      rows.length = boundary;
    }
    let characters = 16_000;
    let mediaLeft = 4;
    const messages = rows.map((revision) => {
      const text = revision.text.slice(0, Math.min(4_000, characters));
      characters -= text.length;
      const originalMedia = decode(revision.media, MediaSchema);
      const media = originalMedia.slice(0, mediaLeft);
      mediaLeft -= media.length;
      const metadata = boundedMetadata(decode(revision.metadata, ObjectSchema));
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
      };
    });
    const snapshot: Snapshot = {
      id: randomUUID(),
      chatId: subject.chatId,
      userId: subject.userId,
      generation: subject.generation,
      version: subject.version,
      messages,
    };
    context.run(
      `
  INSERT INTO snapshots(id,chatId,userId,highwater,payload,createdAt)
  VALUES(?,?,?,?,?,?)
`,
      snapshot.id,
      subject.chatId,
      subject.userId,
      rows[rows.length - 1]!.seq,
      encode(snapshot),
      context.now(),
    );
    const id = randomUUID();
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
    );
    row = context.get<ClassificationSnapshotRecord>(
      `
  SELECT j.*,s.payload
  FROM jobs j
  JOIN snapshots s ON s.id=j.snapshotId
  WHERE j.id=?
`,
      id,
    )!;
    context.audit("classification_scheduled", { id, snapshotId: snapshot.id });
  }
  const token = randomUUID();
  context.run(
    `
  UPDATE jobs
  SET state='running',attempt=attempt+1,token=?
  WHERE id=?
`,
    token,
    row.id,
  );
  context.startAttempt("classification", row.id, row.attempt + 1, token);
  return {
    id: row.id,
    token,
    attempt: row.attempt + 1,
    snapshot: decode(row.payload, SnapshotSchema),
  };
}

export function completeClassification(
  context: StorageContext,
  job: ClassificationJob,
  result: Classification,
): boolean {
  result = Schema.decodeUnknownSync(ClassificationSchema)(result);
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
  );
  if (!current) {
    return false;
  }
  const subject = context.subject(job.snapshot.chatId, job.snapshot.userId);
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
    );
    context.finishAttempt("classification", job.id, job.token, "stale");
    return false;
  }
  const validIds = new Set(job.snapshot.messages.map((message) => message.messageId));
  if (
    !["spam", "clean", "uncertain"].includes(result.verdict) ||
    !result.reason ||
    !result.model ||
    !result.promptVersion ||
    result.evidenceMessageIds.some((id) => !validIds.has(id))
  ) {
    throw new AppError({
      category: "invalid",
      message: "Classifier returned invalid evidence or verdict",
    });
  }
  const id = randomUUID();
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
  );
  const highwater = context.get<{
    highwater: number;
  }>(
    `
  SELECT highwater
  FROM snapshots
  WHERE id=?
`,
    job.snapshot.id,
  )!.highwater;
  context.run(
    `
  UPDATE subjects
  SET processedSeq=MAX(processedSeq,?)
  WHERE chatId=?
  AND userId=?
`,
    highwater,
    subject.chatId,
    subject.userId,
  );
  context.run(
    `
  UPDATE jobs
  SET state='done',token=''
  WHERE id=?
`,
    job.id,
  );
  context.finishAttempt("classification", job.id, job.token, "confirmed");
  if (result.verdict === "spam") {
    const mode = context.settings.mode === "shadow" ? "shadow" : current.mode;
    if (mode === "live" || context.settings.mode === "shadow") {
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
      );
    }
    addAction(context, {
      kind: "ban",
      decisionId: id,
      subject: subject,
      payload: {},
      retryAt: 0,
      originatingMode: mode,
    });
    // Include messages that arrived during classification; one ban, all known cleanup.
    for (const message of context.all<{
      chatId: number;
      messageId: number;
    }>(
      `
  SELECT chatId,messageId
  FROM messages
  WHERE chatId IN (SELECT aliasId
  FROM chat_aliases
  WHERE canonicalId=?)
  AND userId=?
  AND deleted=0
`,
      subject.chatId,
      subject.userId,
    )) {
      addAction(context, {
        kind: "delete",
        decisionId: id,
        subject: subject,
        messageId: message.messageId ?? undefined,
        payload: { reason: "spam_cleanup", messageChatId: message.chatId },
      });
    }
    addAction(context, {
      kind: "notify",
      decisionId: id,
      subject: subject,
      payload: { reason: result.reason.slice(0, 500) },
    });
  }
  context.audit("decision", { id, snapshotId: job.snapshot.id, ...result });
  return true;
}

export function failClassification(
  context: StorageContext,
  job: ClassificationJob,
  error: AppError,
): void {
  const state =
    error.category === "permanent" || error.category === "invalid" ? "blocked" : "retry";
  const delay =
    state === "blocked"
      ? context.settings.recoveryIntervalMs
      : context.delay(job.attempt, context.settings.maxClassificationAttempts);
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
    context.finishAttempt("classification", job.id, job.token, state, error.message);
    context.audit("classification_failed", {
      id: job.id,
      attempt: job.attempt,
      state,
      error: error.message,
    });
  }
}

/** Bound classifier context without changing the archived full-fidelity observation. */
function boundedMetadata(metadata: Record<string, unknown>): Record<string, unknown> {
  let remaining = 8000;
  function bound(value: unknown, depth: number): unknown {
    if (remaining <= 0 || depth > 5) {
      return null;
    }
    if (typeof value === "string") {
      const text = value.slice(0, Math.min(remaining, 2000));
      remaining -= text.length;
      return text;
    }
    if (Array.isArray(value)) {
      return value.slice(0, 32).map((item) => bound(item, depth + 1));
    }
    if (value && typeof value === "object") {
      const result: Record<string, unknown> = {};
      for (const [key, child] of Object.entries(value).slice(0, 32)) {
        remaining -= key.length;
        result[key] = bound(child, depth + 1);
      }
      return result;
    }
    return value;
  }
  const result: Record<string, unknown> = Schema.decodeUnknownSync(ObjectSchema)(
    bound(metadata, 0),
  );
  if (encode(result) !== encode(metadata)) {
    result.evidenceTruncated = true;
  }
  return result;
}
