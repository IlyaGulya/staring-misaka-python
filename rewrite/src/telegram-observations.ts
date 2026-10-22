import type { ChatMemberUpdated, Message, Update } from "grammy/types";
import type { Command, MediaRef, Observation } from "./domain";
import { isCommandText } from "./command-text";

const contentFields = [
  "text",
  "caption",
  "photo",
  "document",
  "video",
  "animation",
  "audio",
  "voice",
  "video_note",
  "sticker",
  "contact",
  "location",
  "venue",
  "poll",
  "dice",
  "game",
  "invoice",
  "paid_media",
  "story",
] as const;

function messageMedia(message: Message): MediaRef[] {
  const photo = message.photo?.at(-1);
  if (photo) {
    return [
      { kind: "photo", fileId: photo.file_id, size: photo.file_size, mimeType: "image/jpeg" },
    ];
  }
  for (const kind of ["document", "video"] as const) {
    const media = message[kind];
    if (media) {
      return [{ kind, fileId: media.file_id, size: media.file_size, mimeType: media.mime_type }];
    }
  }
  const unsupported = [
    "animation",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "paid_media",
    "story",
  ] as const;
  return unsupported.some((field) => message[field] !== undefined) ? [{ kind: "unsupported" }] : [];
}

function normalizeMembership(update: Update, change: ChatMemberUpdated): Observation {
  const member = change.new_chat_member;
  let membership: Observation["membership"] = "joined";
  if (member.status === "kicked") {
    membership = "banned";
  } else if (member.status === "left" || (member.status === "restricted" && !member.is_member)) {
    membership = change.old_chat_member.status === "kicked" ? "unbanned" : "left";
  }
  return {
    source: "bot",
    sourceKey: `bot:${update.update_id}`,
    kind: "membership",
    chatId: change.chat.id,
    userId: member.user.id,
    actorKind: "user",
    occurredAt: change.date * 1000,
    membership,
    changedBy: change.from.id,
    raw: update,
  };
}

function messageMetadata(message: Message, text: string): NonNullable<Observation["metadata"]> {
  const sender = message.from;
  const external = message.external_reply;
  const channelOrigin = external?.origin.type === "channel" ? external.origin : undefined;
  const externalChat = external?.chat ?? channelOrigin?.chat;
  const externalReply =
    externalChat === undefined
      ? null
      : {
          chatId: externalChat.id,
          chatType: externalChat.type,
          messageId: external?.message_id ?? channelOrigin?.message_id,
          text: message.quote?.text,
        };
  const content: Record<string, unknown> = {};
  for (const field of [
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
    "game",
    "invoice",
  ] as const) {
    if (message[field] !== undefined) content[field] = message[field];
  }
  return {
    isCommand: isCommandText(text),
    isBot: sender?.is_bot === true,
    viaBot: message.via_bot ?? null,
    sender: {
      username: sender?.username ?? null,
      firstName: sender?.first_name ?? null,
      lastName: sender?.last_name ?? null,
    },
    externalReply,
    replyText: message.reply_to_message?.text ?? message.reply_to_message?.caption ?? null,
    entities: message.entities ?? message.caption_entities ?? [],
    buttons: message.reply_markup?.inline_keyboard ?? [],
    forward: message.forward_origin ?? null,
    senderChat: message.sender_chat ?? null,
    replyToMessageId: message.reply_to_message?.message_id ?? null,
    mediaGroupId: message.media_group_id ?? null,
    content,
  };
}

export function normalizeUpdate(update: Update): Observation[] {
  const membership = update.chat_member ?? update.my_chat_member;
  if (membership) return [normalizeMembership(update, membership)];

  const message =
    update.message ?? update.edited_message ?? update.channel_post ?? update.edited_channel_post;
  if (!message) return [];

  const base = {
    source: "bot" as const,
    sourceKey: `bot:${update.update_id}`,
    chatId: message.chat.id,
    messageId: message.message_id,
    occurredAt: message.date * 1000,
    raw: update,
  };
  if (message.migrate_to_chat_id !== undefined || message.migrate_from_chat_id !== undefined) {
    return [
      {
        ...base,
        kind: "migration",
        chatId: message.migrate_from_chat_id ?? message.chat.id,
        migratedTo: message.migrate_to_chat_id ?? message.chat.id,
      },
    ];
  }
  if (message.new_chat_members) {
    return message.new_chat_members.map((member, index) => ({
      ...base,
      sourceKey: `${base.sourceKey}:join:${index}`,
      kind: "membership",
      userId: member.id,
      actorKind: "user",
      membership: "joined",
      changedBy: message.from?.id,
    }));
  }
  if (message.left_chat_member) {
    return [
      {
        ...base,
        kind: "membership",
        userId: message.left_chat_member.id,
        actorKind: "user",
        membership: "left",
        changedBy: message.from?.id,
      },
    ];
  }
  // Other service messages must not become classification evidence.
  if (!contentFields.some((field) => message[field] !== undefined)) return [];

  const text = message.text ?? message.caption ?? "";
  const sender = message.from;
  let actorKind: Observation["actorKind"] = "unknown";
  if (message.sender_chat) actorKind = "channel";
  else if (sender) actorKind = "user";

  return [
    {
      ...base,
      kind: update.edited_message || update.edited_channel_post ? "edit" : "message",
      userId: message.sender_chat ? undefined : sender?.id,
      actorKind,
      username: sender?.username,
      displayName: [sender?.first_name, sender?.last_name].filter(Boolean).join(" "),
      text,
      editedAt: message.edit_date === undefined ? undefined : message.edit_date * 1000,
      media: messageMedia(message),
      metadata: messageMetadata(message, text),
    },
  ];
}

export function commandFromUpdate(update: Update): Command | undefined {
  if (update.callback_query) {
    const callback = update.callback_query;
    if (!callback.data?.startsWith("undo:") || !callback.message || callback.message.date === 0) {
      return undefined;
    }
    return {
      id: `bot:${update.update_id}`,
      chatId: callback.message.chat.id,
      userId: callback.from.id,
      text: callback.data,
      messageId: callback.message.message_id,
      callbackId: callback.id,
    };
  }
  const message = update.message;
  if (!message?.from || message.sender_chat || !message.text || !isCommandText(message.text)) {
    return undefined;
  }
  return {
    id: `bot:${update.update_id}`,
    chatId: message.chat.id,
    userId: message.from.id,
    text: message.text,
    messageId: message.message_id,
    replyToMessageId: message.reply_to_message?.message_id,
    replyToUserId: message.reply_to_message?.sender_chat
      ? undefined
      : message.reply_to_message?.from?.id,
  };
}
