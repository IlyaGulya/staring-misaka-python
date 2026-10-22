import type { Database, SQLQueryBindings } from "bun:sqlite";
import type { Settings } from "../domain";
import type { SubjectRecord } from "./records";
import { encode } from "./codecs";
import { openDatabase } from "./database";

/** Owns the connection, clock and identity. Mutation helpers join their caller’s transaction.
 * Domain workflows are deliberately not methods on this context.
 */
export class StorageContext {
  readonly db: Database;
  botId = 0;
  botUsername = "";

  constructor(
    readonly settings: Settings,
    readonly now: () => number,
  ) {
    this.db = openDatabase(settings.dbPath);
  }

  get<Projection>(sql: string, ...args: SQLQueryBindings[]): Projection | undefined {
    return this.db.query<Projection, SQLQueryBindings[]>(sql).get(...args) ?? undefined;
  }

  all<Projection>(sql: string, ...args: SQLQueryBindings[]): Projection[] {
    return this.db.query<Projection, SQLQueryBindings[]>(sql).all(...args);
  }

  canonicalChatId(chatId: number): number {
    const alias = this.get<{ canonicalId: number }>(
      `
      SELECT canonicalId FROM chat_aliases WHERE aliasId = ?
    `,
      chatId,
    );
    return alias?.canonicalId ?? chatId;
  }

  resolveChatId(chatId: number): number {
    const route = this.get<{ destinationId: number }>(
      `
      SELECT destinationId FROM chat_routes WHERE canonicalId = ?
    `,
      this.canonicalChatId(chatId),
    );
    return route?.destinationId ?? chatId;
  }

  tracksChat(chatId: number): boolean {
    const canonical = this.canonicalChatId(chatId);
    return this.settings.chatIds.some(
      (configured) => this.canonicalChatId(configured) === canonical,
    );
  }

  run(sql: string, ...args: SQLQueryBindings[]): number {
    return this.db.query(sql).run(...args).changes;
  }

  atomic<T>(body: () => T): T {
    return this.db.transaction(body).immediate();
  }

  audit(kind: string, payload: unknown): void {
    this.run(
      `
  INSERT INTO audit(kind,payload,createdAt)
  VALUES(?,?,?)
`,
      kind,
      encode(payload),
      this.now(),
    );
  }

  enroll(chatId: number, userId: number): void {
    chatId = this.canonicalChatId(chatId);
    this.run(
      `
  INSERT OR IGNORE INTO chat_aliases(aliasId,canonicalId)
  VALUES(?,?)
`,
      chatId,
      chatId,
    );
    this.run(
      `
  INSERT OR IGNORE INTO chats(chatId,enabled)
  VALUES(?,?)
`,
      chatId,
      Number(this.tracksChat(chatId)),
    );
    if (
      this.run(
        `
  INSERT OR IGNORE INTO subjects(chatId,userId)
  VALUES(?,?)
`,
        chatId,
        userId,
      )
    ) {
      this.audit("subject_enrolled", { chatId, userId });
    }
  }

  subject(chatId: number, userId: number): SubjectRecord {
    return this.get<SubjectRecord>(
      `
      SELECT * FROM subjects WHERE chatId = ? AND userId = ?
    `,
      this.canonicalChatId(chatId),
      userId,
    )!;
  }

  highwater(chatId: number, userId: number): number {
    const latest = this.get<{ n: number }>(
      `
      SELECT COALESCE(MAX(seq), 0) AS n
      FROM revisions
      WHERE chatId IN (SELECT aliasId FROM chat_aliases WHERE canonicalId = ?)
        AND userId = ?
    `,
      this.canonicalChatId(chatId),
      userId,
    )!;
    return latest.n;
  }

  delay(attempt: number, maximum: number): number {
    if (attempt >= maximum) {
      return this.settings.recoveryIntervalMs;
    }
    const backoff = this.settings.retryBaseMs * 2 ** Math.min(attempt - 1, 20);
    return Math.min(this.settings.recoveryIntervalMs, backoff);
  }

  startAttempt(
    workType: "action" | "classification",
    id: string,
    attempt: number,
    token: string,
  ): void {
    this.run(
      `
      INSERT INTO attempts(workType, workId, attempt, token, startedAt)
      VALUES (?, ?, ?, ?, ?)
    `,
      workType,
      id,
      attempt,
      token,
      this.now(),
    );
  }

  finishAttempt(
    workType: "action" | "classification",
    id: string,
    token: string,
    outcome: string,
    error: string | null = null,
  ): void {
    this.run(
      `
      UPDATE attempts
      SET finishedAt = ?, outcome = ?, error = ?
      WHERE workType = ? AND workId = ? AND token = ? AND finishedAt IS NULL
    `,
      this.now(),
      outcome,
      error,
      workType,
      id,
      token,
    );
  }
}
