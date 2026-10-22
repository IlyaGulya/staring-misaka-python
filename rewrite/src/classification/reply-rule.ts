import { Schema } from "effect";
import type { Api } from "grammy";
import { AppError, type Classification, type Snapshot } from "../domain.ts";

const ExternalReply = Schema.Struct({
  chatId: Schema.Number,
  chatType: Schema.Literals(["channel", "supergroup", "channel_peer"]),
});
const TelegramChat = Schema.Struct({
  id: Schema.Number,
  type: Schema.Literals(["group", "supergroup", "channel"]),
  linked_chat_id: Schema.optional(Schema.Number),
});
const decodeExternalReply = Schema.decodeUnknownSync(ExternalReply);
const decodeChat = Schema.decodeUnknownSync(TelegramChat);

export interface VerifiedChat {
  readonly id: number;
  readonly linkedChatId?: number;
}

type ReplyRuleResult =
  | { readonly kind: "matched"; readonly classification: Classification }
  | { readonly kind: "continue"; readonly verifiedChat?: VerifiedChat };

/** Unknown linked-channel state must never be interpreted as proof of an unrelated reply. */
export async function evaluateReplyRule(
  snapshot: Snapshot,
  api: Api,
  signal: AbortSignal,
): Promise<ReplyRuleResult> {
  const externalReplies: { messageId: number; chatId: number }[] = [];
  for (const message of snapshot.messages) {
    try {
      const reply = decodeExternalReply(message.metadata.externalReply);
      if (
        Number.isSafeInteger(reply.chatId) &&
        reply.chatId < 0 &&
        reply.chatId !== snapshot.chatId
      ) {
        externalReplies.push({ messageId: message.messageId, chatId: reply.chatId });
      }
    } catch {
      // Missing or unrecognized metadata is left to the ordinary evidence pipeline.
    }
  }
  if (externalReplies.length === 0) return { kind: "continue" };

  let verifiedChat: VerifiedChat;
  try {
    const chat = decodeChat(await api.getChat(snapshot.chatId, signal));
    const linkedChatId = chat.linked_chat_id;
    const validLinkedId =
      linkedChatId === undefined || (Number.isSafeInteger(linkedChatId) && linkedChatId < 0);
    if (chat.id !== snapshot.chatId || !validLinkedId) return { kind: "continue" };
    verifiedChat = { id: chat.id, linkedChatId };
  } catch {
    if (signal.aborted) {
      throw new AppError({
        category: "transient",
        message: "Classification cancelled or timed out",
      });
    }
    return { kind: "continue" };
  }

  const evidenceIds = new Set<number>();
  for (const reply of externalReplies) {
    if (reply.chatId !== verifiedChat.linkedChatId) evidenceIds.add(reply.messageId);
  }
  if (evidenceIds.size === 0) return { kind: "continue", verifiedChat };

  return {
    kind: "matched",
    classification: {
      verdict: "spam",
      reason: "Reply to an unrelated channel",
      evidenceMessageIds: [...evidenceIds],
      model: "rule/cross-channel-reply",
      promptVersion: "cross-channel-reply-v1",
    },
  };
}
