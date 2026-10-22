import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import { realpathSync, statSync } from "node:fs";
import { Schema } from "effect";
import { AppError, type Classification, type Snapshot } from "../domain";
import { StorageContext } from "./context";
import { encode, decode, ObjectSchema } from "./codecs";
import { invalidateJobs } from "./subject-lifecycle";
import { ingestObservation } from "./ingestion";

/** Hold a source read transaction around one target write transaction.
 * Archiving source rows and importing their effects must commit together.
 */
export function importLegacy(context: StorageContext, path: string): Record<string, number> {
  const sourcePath = realpathSync(path);
  if (context.settings.dbPath !== ":memory:") {
    const sourceStat = statSync(sourcePath);
    const targetStat = statSync(context.settings.dbPath);
    if (
      sourcePath === realpathSync(context.settings.dbPath) ||
      (sourceStat.dev === targetStat.dev && sourceStat.ino === targetStat.ino)
    ) {
      throw new AppError({
        category: "invalid",
        message: "Legacy source must not be target database",
      });
    }
  }
  const source = new Database(path, { readonly: true, strict: true });
  try {
    const tables = new Set(
      source
        .query<
          {
            name: string;
          },
          []
        >(
          `
  SELECT name
  FROM sqlite_master
  WHERE type='table'
`,
        )
        .all()
        .map((row) => row.name),
    );
    if (
      !["new_users", "approved_users", "message_queue", "spam_check_results"].some((table) =>
        tables.has(table),
      )
    ) {
      throw new AppError({
        category: "invalid",
        message: "Source is not a recognized legacy Misaka database",
      });
    }
    return source.transaction(() =>
      context.atomic(() => {
        const counts: Record<string, number> = {};
        const importTable = <
          T extends {
            readonly id: string | number;
          },
        >(
          table: string,
          schema: Schema.Codec<T>,
          apply: (row: T) => void,
        ): void => {
          counts[table] = 0;
          if (!tables.has(table)) {
            return;
          }
          for (const raw of source
            .query<Record<string, unknown>, []>(
              `
  SELECT * FROM ${table}
`,
            )
            .all()) {
            const row = Schema.decodeUnknownSync(schema)(raw);
            if (
              !context.run(
                `
  INSERT OR IGNORE INTO legacy_records(tableName,legacyId,payload)
  VALUES(?,?,?)
`,
                table,
                String(row.id),
                encode(raw),
              )
            ) {
              continue;
            }
            apply(row);
            counts[table]!++;
          }
        };
        const legacyKey = { id: Schema.Union([Schema.String, Schema.Number]) };
        const legacySubject = { ...legacyKey, chat_id: Schema.Number, user_id: Schema.Number };
        importTable(
          "group_settings",
          Schema.Struct({ ...legacyKey, chat_id: Schema.Number, enabled: Schema.Number }),
          (row) => {
            context.run(
              `
  INSERT INTO chats(chatId,enabled)
  VALUES(?,?)
  ON CONFLICT(chatId)
  DO UPDATE SET enabled=excluded.enabled
`,
              row.chat_id,
              Number(Boolean(row.enabled)),
            );
          },
        );
        importTable("new_users", Schema.Struct(legacySubject), (row) =>
          context.enroll(row.chat_id, row.user_id),
        );
        importTable("approved_users", Schema.Struct(legacySubject), (row) => {
          context.enroll(row.chat_id, row.user_id);
          invalidateJobs(context, row.chat_id, row.user_id);
          context.run(
            `
  UPDATE subjects
  SET approved=COALESCE(approved,'legacy'),version=version+1
  WHERE chatId=?
  AND userId=?
`,
            row.chat_id,
            row.user_id,
          );
          context.audit("approved", {
            chatId: row.chat_id,
            userId: row.user_id,
            origin: "legacy",
            legacyId: row.id,
          });
        });
        importTable(
          "spam_check_results",
          Schema.Struct({
            ...legacySubject,
            is_spam: Schema.Number,
            reason: Schema.String,
            model: Schema.optional(Schema.NullOr(Schema.String)),
            checked_at: Schema.String,
          }),
          (row) => {
            context.enroll(row.chat_id, row.user_id);
            const id = legacyId("spam_check_results", row.id);
            const snapshot: Snapshot = {
              id,
              chatId: row.chat_id,
              userId: row.user_id,
              version: 0,
              generation: 0,
              messages: [],
            };
            context.run(
              `
  INSERT INTO snapshots(id,chatId,userId,highwater,payload,createdAt)
  VALUES(?,?,?,?,?,?)
`,
              id,
              row.chat_id,
              row.user_id,
              0,
              encode(snapshot),
              context.now(),
            );
            const classification: Classification = {
              verdict: row.is_spam ? "spam" : "clean",
              reason: row.reason || "Legacy verdict",
              evidenceMessageIds: [],
              model: row.model || "legacy",
              promptVersion: "legacy",
            };
            context.run(
              `
  INSERT INTO decisions(id,chatId,userId,snapshotId,payload,createdAt,status)
  VALUES(?,?,?,?,?,?,'legacy')
`,
              id,
              row.chat_id,
              row.user_id,
              id,
              encode(classification),
              Date.parse(row.checked_at) || context.now(),
            );
          },
        );
        importTable(
          "message_queue",
          Schema.Struct({
            ...legacySubject,
            message_id: Schema.Number,
            message_text: Schema.String,
            raw_message_text: Schema.optional(Schema.NullOr(Schema.String)),
            message_metadata: Schema.optional(Schema.NullOr(Schema.String)),
            created_at: Schema.String,
            status: Schema.String,
          }),
          (row) => {
            let metadata: Record<string, unknown> = {};
            if (typeof row.message_metadata === "string") {
              try {
                metadata = decode(row.message_metadata, ObjectSchema);
              } catch {
                metadata = { legacyMetadata: row.message_metadata };
              }
            }
            ingestObservation(context, {
              source: "replay",
              sourceKey: `legacy:message_queue:${row.id}`,
              kind: "message",
              chatId: row.chat_id,
              userId: row.user_id,
              actorKind: "user",
              messageId: row.message_id,
              text: row.raw_message_text ?? row.message_text,
              occurredAt: Date.parse(row.created_at) || context.now(),
              metadata,
            });
            if (row.status === "completed") {
              context.run(
                `
  UPDATE revisions
  SET eligible=0
  WHERE chatId=?
  AND messageId=?
`,
                row.chat_id,
                row.message_id,
              );
            }
          },
        );
        importTable(
          "banned_users",
          Schema.Struct({
            ...legacySubject,
            spam_check_id: Schema.optional(Schema.NullOr(Schema.String)),
          }),
          (row) => {
            context.enroll(row.chat_id, row.user_id);
            context.audit("legacy_ban", {
              legacyId: row.id,
              chatId: row.chat_id,
              userId: row.user_id,
              ownership: "unknown",
              spamCheckId: row.spam_check_id,
            });
          },
        );
        importTable(
          "pending_ban_requests",
          Schema.Struct({
            ...legacyKey,
            original_chat_id: Schema.Number,
            original_message_id: Schema.Number,
            sender_id: Schema.Number,
            message_text: Schema.String,
            created_at: Schema.String,
          }),
          (row) => {
            ingestObservation(context, {
              source: "replay",
              sourceKey: `legacy:pending_ban_requests:${row.id}`,
              kind: "message",
              chatId: row.original_chat_id,
              messageId: row.original_message_id,
              userId: row.sender_id,
              actorKind: "user",
              text: row.message_text,
              occurredAt: Date.parse(row.created_at) || context.now(),
              metadata: { legacyManualRequest: String(row.id) },
            });
            context.audit("legacy_manual_request_requeued", {
              legacyId: row.id,
              chatId: row.original_chat_id,
              userId: row.sender_id,
            });
          },
        );
        importTable("admin_settings", Schema.Struct(legacyKey), (row) => {
          context.audit("legacy_admin_settings_archived", { legacyId: row.id });
        });
        context.audit("legacy_import", counts);
        return counts;
      }),
    )();
  } finally {
    source.close();
  }
}

const legacyId = (table: string, id: unknown): string => {
  const hash = createHash("sha256")
    .update(`misaka-legacy:${table}:${String(id)}`)
    .digest("hex");
  return `${hash.slice(0, 8)}-${hash.slice(8, 12)}-5${hash.slice(13, 16)}-a${hash.slice(17, 20)}-${hash.slice(20, 32)}`;
};
