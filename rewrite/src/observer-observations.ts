import { createHash } from "node:crypto";
import type { MediaRef, Observation } from "./domain";
import { isCommandText } from "./command-text";

type RuntimeObject = Record<string, unknown>;

function isObject(value: unknown): value is RuntimeObject {
  return typeof value === "object" && value !== null;
}

function object(value: unknown): RuntimeObject {
  return isObject(value) ? value : {};
}

function integer(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined;
  // GramJS IDs may be numbers, native bigints, or big-integer instances.
  const result = Number(String(value));
  return Number.isSafeInteger(result) ? result : undefined;
}

function peerId(value: unknown): number | undefined {
  const peer = object(value);
  if (peer.channelId !== undefined) {
    const id = integer(peer.channelId);
    return id === undefined ? undefined : -1_000_000_000_000 - id;
  }
  if (peer.chatId !== undefined) {
    const id = integer(peer.chatId);
    return id === undefined ? undefined : -id;
  }
  return integer(peer.userId);
}

function jsonData(value: unknown): unknown {
  if (value === undefined) return null;
  return JSON.parse(
    JSON.stringify(value, (key, item: unknown) => {
      if (key.startsWith("_")) return undefined;
      return typeof item === "bigint" ? String(item) : item;
    }),
  );
}

interface MessageContext {
  message: RuntimeObject;
  base: Pick<Observation, "source" | "sourceKey" | "chatId" | "messageId" | "occurredAt" | "raw">;
  userId: number | undefined;
  editDate: number | undefined;
}

function messageContext(value: unknown): MessageContext | undefined {
  const message = object(value);
  const chatId = peerId(message.peerId);
  const messageId = integer(message.id);
  const date = integer(message.date);
  if (chatId === undefined || messageId === undefined || date === undefined) return undefined;

  const editDate = integer(message.editDate);
  const senderId = integer(object(message.fromId).userId);
  const userId = senderId ?? (chatId > 0 && message.out !== true ? chatId : undefined);
  // Telegram revision timestamps have second precision. Hash content to preserve same-second edits.
  const content = {
    text: message.message ?? "",
    entities: jsonData(message.entities),
    media: jsonData(message.media),
    buttons: jsonData(message.replyMarkup),
    forward: jsonData(message.fwdFrom),
    action: jsonData(message.action),
    reply: jsonData(message.replyTo),
  };
  const contentHash = createHash("sha256")
    .update(JSON.stringify(content))
    .digest("hex")
    .slice(0, 24);
  return {
    message,
    userId,
    editDate,
    base: {
      source: "observer",
      sourceKey: `observer:${chatId}:${messageId}:${editDate ?? date}:${contentHash}`,
      chatId,
      messageId,
      occurredAt: date * 1000,
      raw: jsonData(value),
    },
  };
}

function serviceObservations({ message, base, userId }: MessageContext): Observation[] {
  const action = object(message.action);
  switch (action.className) {
    case "MessageActionChatAddUser":
      if (!Array.isArray(action.users)) return [];
      return action.users.map((id, index) => ({
        ...base,
        sourceKey: `${base.sourceKey}:join:${index}`,
        kind: "membership",
        userId: integer(id),
        actorKind: "user",
        membership: "joined",
        changedBy: userId,
      }));
    case "MessageActionChatJoinedByLink":
    case "MessageActionChatJoinedByRequest":
      return [
        {
          ...base,
          kind: "membership",
          userId,
          actorKind: "user",
          membership: "joined",
          changedBy: integer(action.inviterId),
        },
      ];
    case "MessageActionChatDeleteUser":
      return [
        {
          ...base,
          kind: "membership",
          userId: integer(action.userId),
          actorKind: "user",
          membership: "left",
          changedBy: userId,
        },
      ];
    case "MessageActionChatMigrateTo":
      return [{ ...base, kind: "migration", migratedTo: peerId({ channelId: action.channelId }) }];
    case "MessageActionChannelMigrateFrom":
      return [
        {
          ...base,
          kind: "migration",
          chatId: peerId({ chatId: action.chatId }) ?? base.chatId,
          migratedTo: base.chatId,
        },
      ];
    default:
      return [];
  }
}

function messageMedia(value: unknown): MediaRef[] {
  const attachment = object(value);
  switch (attachment.className) {
    case "MessageMediaPhoto":
      return [{ kind: "photo", mimeType: "image/jpeg" }];
    case "MessageMediaDocument": {
      const document = object(attachment.document);
      return [
        {
          kind: "document",
          mimeType: typeof document.mimeType === "string" ? document.mimeType : undefined,
          size: integer(document.size),
        },
      ];
    }
    case "MessageMediaEmpty":
    case "MessageMediaWebPage":
      return [];
    default:
      return value ? [{ kind: "unsupported" }] : [];
  }
}

function messageEntities(value: unknown): RuntimeObject[] {
  if (!Array.isArray(value)) return [];
  return value.map((value) => {
    const entity = object(value);
    const type = String(entity.className ?? "")
      .replace(/^MessageEntity/, "")
      .replace(/[A-Z]/g, (letter, index: number) => `${index ? "_" : ""}${letter.toLowerCase()}`);
    return {
      ...object(jsonData(value)),
      type,
      offset: entity.offset,
      length: entity.length,
      ...(entity.userId === undefined ? {} : { userId: integer(entity.userId) }),
    };
  });
}

function messageButtons(value: unknown): RuntimeObject[][] {
  const rows = object(value).rows;
  if (!Array.isArray(rows)) return [];
  return rows.map((row) => {
    const buttons = object(row).buttons;
    if (!Array.isArray(buttons)) return [];
    return buttons.map((button) => {
      const item = object(button);
      return {
        ...object(jsonData(button)),
        text: item.text,
        url: item.url,
        ...(item.data instanceof Uint8Array
          ? { callback_data: Buffer.from(item.data).toString("utf8") }
          : {}),
      };
    });
  });
}

function messageMetadata(
  message: RuntimeObject,
  text: string,
): NonNullable<Observation["metadata"]> {
  const sender = object(message.sender);
  const reply = object(message.replyTo);
  const replyPeer = object(reply.replyToPeerId);
  const externalChatId = peerId(replyPeer);
  let chatType = "private";
  if (replyPeer.channelId !== undefined) chatType = "channel_peer";
  else if (replyPeer.chatId !== undefined) chatType = "group";

  return {
    isCommand: isCommandText(text),
    isBot: sender.bot === true,
    viaBotId: integer(message.viaBotId) ?? null,
    viaBot: jsonData(message.viaBot),
    sender: {
      username: sender.username ?? null,
      firstName: sender.firstName ?? null,
      lastName: sender.lastName ?? null,
    },
    externalReply:
      externalChatId === undefined
        ? null
        : {
            chatId: externalChatId,
            chatType,
            messageId: integer(reply.replyToMsgId),
            text: typeof reply.quoteText === "string" ? reply.quoteText : undefined,
          },
    entities: messageEntities(message.entities),
    buttons: messageButtons(message.replyMarkup),
    forward: jsonData(message.fwdFrom),
    replyToMessageId: reply.replyToMsgId ?? null,
    mediaGroupId: message.groupedId === undefined ? null : String(message.groupedId),
    media: jsonData(message.media),
    senderPeer: jsonData(message.fromId),
  };
}

/** Uses Telegram's revision clock, never receipt time. Does not download or mutate remote state. */
export function normalizeObserverMessage(value: unknown, edited = false): Observation[] {
  const context = messageContext(value);
  if (!context) return [];
  const { message, base, userId, editDate } = context;
  if (message.action) return serviceObservations(context);

  const sender = object(message.sender);
  const text = typeof message.message === "string" ? message.message : "";
  let actorKind: Observation["actorKind"] = "unknown";
  if (userId !== undefined) actorKind = "user";
  else if (object(message.fromId).channelId !== undefined || message.post === true)
    actorKind = "channel";

  return [
    {
      ...base,
      kind: edited ? "edit" : "message",
      userId,
      actorKind,
      username: typeof sender.username === "string" ? sender.username : undefined,
      displayName: [sender.firstName, sender.lastName].filter(Boolean).join(" "),
      text,
      editedAt: editDate === undefined ? undefined : editDate * 1000,
      media: messageMedia(message.media),
      metadata: messageMetadata(message, text),
    },
  ];
}

interface RawObservations {
  observations: Observation[];
  unscopedDeletions: number;
}

function deletionObservations(
  update: RuntimeObject,
  nonChannelMessages: ReadonlyMap<number, number>,
  now: number,
): RawObservations {
  if (!Array.isArray(update.messages)) return { observations: [], unscopedDeletions: 0 };
  const byChat = new Map<number, number[]>();
  let unscopedDeletions = 0;
  for (const id of update.messages) {
    const messageId = integer(id);
    if (messageId === undefined) continue;
    const chatId =
      update.channelId === undefined
        ? nonChannelMessages.get(messageId)
        : peerId({ channelId: update.channelId });
    // Telegram omits chat identity for private/basic-group deletions. Never guess.
    if (chatId === undefined) {
      unscopedDeletions++;
      continue;
    }
    const ids = byChat.get(chatId) ?? [];
    ids.push(messageId);
    byChat.set(chatId, ids);
  }
  const observations: Observation[] = [];
  for (const [chatId, deletedIds] of byChat) {
    observations.push({
      source: "observer",
      sourceKey: `observer:delete:${chatId}:${String(update.pts)}:${deletedIds.join(",")}`,
      kind: "delete",
      chatId,
      occurredAt: now,
      deletedIds,
      raw: jsonData(update),
    });
  }
  return { observations, unscopedDeletions };
}

function participantObservations(update: RuntimeObject, now: number): Observation[] {
  const chatId =
    update.channelId === undefined
      ? peerId({ chatId: update.chatId })
      : peerId({ channelId: update.channelId });
  if (chatId === undefined) return [];

  const participant = object(update.newParticipant);
  const previous = object(update.prevParticipant);
  const banned = object(participant.bannedRights).viewMessages === true;
  const previouslyBanned = object(previous.bannedRights).viewMessages === true;
  const left =
    update.className === "UpdateChatParticipantDelete" ||
    participant.left === true ||
    (update.className !== "UpdateChatParticipantAdd" && !update.newParticipant);
  let membership: Observation["membership"] = "joined";
  if (banned) membership = "banned";
  else if (left) membership = previouslyBanned ? "unbanned" : "left";

  return [
    {
      source: "observer",
      sourceKey: `observer:membership:${chatId}:${String(update.userId)}:${String(update.qts ?? update.version ?? update.date)}`,
      kind: "membership",
      chatId,
      userId: integer(update.userId),
      actorKind: "user",
      occurredAt: (integer(update.date) ?? Math.floor(now / 1000)) * 1000,
      changedBy: integer(update.actorId ?? update.inviterId),
      membership,
      raw: jsonData(update),
    },
  ];
}

/** Normalizes raw service/deletion/participant updates independently of the client lifecycle. */
export function normalizeObserverUpdate(
  value: unknown,
  nonChannelMessages: ReadonlyMap<number, number>,
  now: number,
): RawObservations {
  const update = object(value);
  switch (update.className) {
    case "UpdateNewMessage":
    case "UpdateNewChannelMessage":
      return {
        observations: object(update.message).action ? normalizeObserverMessage(update.message) : [],
        unscopedDeletions: 0,
      };
    case "UpdateDeleteChannelMessages":
    case "UpdateDeleteMessages":
      return deletionObservations(update, nonChannelMessages, now);
    case "UpdateChannelParticipant":
    case "UpdateChatParticipant":
    case "UpdateChatParticipantAdd":
    case "UpdateChatParticipantDelete":
      return { observations: participantObservations(update, now), unscopedDeletions: 0 };
    default:
      return { observations: [], unscopedDeletions: 0 };
  }
}
