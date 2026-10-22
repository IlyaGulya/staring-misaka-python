import { AppError, type Observation } from "../domain";
import { StorageContext } from "./context";
import type { MessageEvidenceRecord } from "./records";
import { encode, decode, ObjectSchema, MediaSchema } from "./codecs";
import { reconcileObservedNotification, addAction } from "./actions";
import { invalidateJobs } from "./subject-lifecycle";

/** Workflows here run inside the Store-owned SQLite transaction unless documented otherwise. */
export function ingestObservation(context: StorageContext, observation: Observation): boolean {
  if (
    !context.run(
      `
  INSERT OR IGNORE INTO observations(source,sourceKey,payload,createdAt)
  VALUES(?,?,?,?)
`,
      observation.source,
      observation.sourceKey,
      encode(observation),
      context.now(),
    )
  ) {
    return false;
  }
  context.audit("observation", observation);
  reconcileObservedNotification(context, observation);
  if (observation.kind === "delete") {
    for (const id of observation.deletedIds ?? []) {
      context.run(
        `
  UPDATE messages
  SET deleted=1
  WHERE chatId=?
  AND messageId=?
`,
        observation.chatId,
        id,
      );
    }
    return true;
  }
  if (observation.kind === "migration") {
    if (observation.migratedTo === undefined) {
      return true;
    }
    const canonical = context.canonicalChatId(observation.chatId);
    if (
      context.get<{
        destinationId: number;
      }>(
        `
  SELECT destinationId
  FROM chat_routes
  WHERE canonicalId=?
`,
        canonical,
      )?.destinationId === observation.migratedTo
    ) {
      return true;
    }
    const existing = context.get<{
      canonicalId: number;
    }>(
      `
  SELECT canonicalId
  FROM chat_aliases
  WHERE aliasId=?
`,
      observation.migratedTo,
    );
    if (
      existing &&
      existing.canonicalId !== canonical &&
      context.get<{ 1: number }>(
        `
          SELECT 1
          FROM subjects
          WHERE chatId=?
          LIMIT 1
        `,
        existing.canonicalId,
      )
    ) {
      throw new AppError({
        category: "invalid",
        message: "Migration conflicts with an already independent chat",
      });
    }
    context.run(
      `
  INSERT OR IGNORE INTO chat_aliases(aliasId,canonicalId)
  VALUES(?,?)
`,
      canonical,
      canonical,
    );
    context.run(
      `
  INSERT INTO chat_aliases(aliasId,canonicalId)
  VALUES(?,?)
  ON CONFLICT(aliasId)
  DO UPDATE SET canonicalId=excluded.canonicalId
`,
      observation.migratedTo,
      canonical,
    );
    context.run(
      `
  INSERT INTO chat_routes(canonicalId,destinationId)
  VALUES(?,?)
  ON CONFLICT(canonicalId)
  DO UPDATE SET destinationId=excluded.destinationId
`,
      canonical,
      observation.migratedTo,
    );
    context.run(
      `
  INSERT OR IGNORE INTO chats(chatId,enabled)
  VALUES(?,?)
`,
      canonical,
      Number(context.tracksChat(canonical)),
    );
    for (const subject of context.all<{
      userId: number;
    }>(
      `
  SELECT userId
  FROM subjects
  WHERE chatId=?
`,
      canonical,
    )) {
      invalidateJobs(context, canonical, subject.userId);
      context.run(
        `
  UPDATE subjects
  SET version=version+1,generation=generation+1,processedSeq=MAX(processedSeq,?)
  WHERE chatId=?
  AND userId=?
`,
        context.highwater(canonical, subject.userId),
        canonical,
        subject.userId,
      );
    }
    context.audit("chat_migrated", {
      canonical,
      from: observation.chatId,
      to: observation.migratedTo,
    });
    return true;
  }
  if (observation.userId !== undefined && (observation.actorKind ?? "user") === "user") {
    context.enroll(observation.chatId, observation.userId);
  }
  if (observation.kind === "membership") {
    if (
      observation.userId !== undefined &&
      (observation.membership === "banned" || observation.membership === "unbanned")
    ) {
      if (observation.changedBy !== context.botId || context.botId === 0) {
        context.run(
          `
  UPDATE subjects
  SET restrictionEpoch=restrictionEpoch+1
  WHERE chatId=?
  AND userId=?
`,
          context.canonicalChatId(observation.chatId),
          observation.userId,
        );
        context.audit("foreign_restriction_change", {
          chatId: observation.chatId,
          userId: observation.userId,
          changedBy: observation.changedBy,
          membership: observation.membership,
        });
      }
    }
    return true;
  }
  if (observation.messageId === undefined) {
    return true;
  }
  const old = context.get<MessageEvidenceRecord>(
    `
  SELECT m.*,r.text,r.media,r.metadata,r.source,r.seq,r.eligible,r.mode
  FROM messages m
  JOIN revisions r USING(chatId,messageId,revision)
  WHERE m.chatId=?
  AND m.messageId=?
`,
    observation.chatId,
    observation.messageId,
  );
  const stamp = observation.editedAt ?? observation.occurredAt;
  if (old && stamp < old.stamp) {
    return true;
  }
  const text = observation.text ?? "";
  const media = observation.media ?? [];
  let mergedMedia = media;
  let metadata = observation.metadata ?? {};
  if (old && stamp === old.stamp && text === old.text) {
    const previous = decode(old.media, MediaSchema);
    if (media.length === 0) {
      mergedMedia = previous;
    } else {
      mergedMedia = media.map((item, index) => {
        const prior = previous[index];
        const fileChanged = item.fileId && prior?.fileId && item.fileId !== prior.fileId;
        const blobChanged = item.blobHash && prior?.blobHash && item.blobHash !== prior.blobHash;
        if (old.source === observation.source && (fileChanged || blobChanged)) {
          return item;
        }
        if (prior?.kind !== item.kind) {
          return item;
        }
        return {
          ...prior,
          ...item,
          fileId: item.fileId ?? prior.fileId,
          blobHash: item.blobHash ?? prior.blobHash,
        };
      });
    }
    const previousMetadata = decode(old.metadata, ObjectSchema);
    if (old.source === "bot" && observation.source !== "bot") {
      metadata = { ...metadata, ...previousMetadata };
    } else {
      metadata = { ...previousMetadata, ...metadata };
    }
    const addedUsableMedia =
      !previous.some((item) => item.fileId || item.blobHash) &&
      mergedMedia.some((item) => item.fileId || item.blobHash);
    const sameSourceEdit =
      observation.kind === "edit" &&
      observation.source === old.source &&
      (encode(mergedMedia) !== old.media || encode(metadata) !== old.metadata);
    if (!addedUsableMedia && !sameSourceEdit) {
      // Bot/observer receipts represent one revision. Rich source metadata is retained;
      // immutable snapshots already captured are deliberately not rewritten.
      context.run(
        `
  UPDATE revisions
  SET media=?,metadata=?,source=?
  WHERE seq=?
`,
        encode(mergedMedia),
        encode(metadata),
        observation.source === "bot" ? "bot" : old.source,
        old.seq,
      );
      return true;
    }
  }
  const revision = (old?.revision ?? 0) + 1;
  const eligible = Number(
    (observation.actorKind ?? "user") === "user" &&
      observation.userId !== undefined &&
      observation.userId !== context.botId &&
      observation.metadata?.isSelf !== true &&
      observation.metadata?.isCommand !== true &&
      observation.metadata?.service !== true,
  );
  let mode = context.settings.mode;
  if (old && stamp === old.stamp && text === old.text && observation.kind !== "edit") {
    mode = old.mode;
  }
  context.run(
    `
  INSERT INTO revisions(chatId,messageId,revision,userId,actorKind,text,media,metadata,source,eligible,mode)
  VALUES(?,?,?,?,?,?,?,?,?,?,?)
`,
    observation.chatId,
    observation.messageId,
    revision,
    observation.userId ?? null,
    observation.actorKind ?? "user",
    text,
    encode(mergedMedia),
    encode(metadata),
    observation.source,
    eligible,
    mode,
  );
  context.run(
    `
  INSERT INTO messages(chatId,messageId,userId,revision,stamp)
  VALUES(?,?,?,?,?)
  ON CONFLICT(chatId,messageId)
  DO UPDATE SET revision=excluded.revision,stamp=excluded.stamp,userId=excluded.userId
`,
    observation.chatId,
    observation.messageId,
    observation.userId ?? null,
    revision,
    stamp,
  );
  if (eligible && observation.userId !== undefined) {
    const subject = context.subject(observation.chatId, observation.userId);
    if (
      subject.activeDecision &&
      !subject.approved &&
      !context.get<{
        1: number;
      }>(
        `
  SELECT 1
  FROM overrides
  WHERE decisionId=?
`,
        subject.activeDecision,
      )
    ) {
      addAction(context, {
        kind: "delete",
        decisionId: subject.activeDecision,
        subject: subject,
        messageId: observation.messageId,
        payload: { reason: "message_during_ban", messageChatId: observation.chatId },
      });
    }
  }
  return true;
}
