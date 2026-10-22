import { Schema } from "effect";

export const encode = (value: unknown): string => JSON.stringify(value);
export const ObjectSchema = Schema.Record(Schema.String, Schema.Unknown);

export const MediaSchema = Schema.Array(
  Schema.Struct({
    kind: Schema.Literals(["photo", "document", "video", "unsupported"]),
    fileId: Schema.optional(Schema.String),
    blobHash: Schema.optional(Schema.String),
    mimeType: Schema.optional(Schema.String),
    size: Schema.optional(Schema.Number),
  }),
);

export const SnapshotSchema = Schema.Struct({
  id: Schema.String,
  chatId: Schema.Number,
  userId: Schema.Number,
  generation: Schema.Number,
  version: Schema.Number,
  messages: Schema.Array(
    Schema.Struct({
      messageId: Schema.Number,
      revision: Schema.Number,
      text: Schema.String,
      media: MediaSchema,
      metadata: ObjectSchema,
    }),
  ),
});

export const ClassificationSchema = Schema.Struct({
  verdict: Schema.Literals(["spam", "clean", "uncertain"]),
  reason: Schema.String,
  evidenceMessageIds: Schema.Array(Schema.Number),
  model: Schema.String,
  promptVersion: Schema.String,
  usage: Schema.optional(Schema.Record(Schema.String, Schema.Number)),
});

export const CommandSchema = Schema.Struct({
  id: Schema.String,
  chatId: Schema.Number,
  userId: Schema.Number,
  text: Schema.String,
  messageId: Schema.optional(Schema.Number),
  replyToMessageId: Schema.optional(Schema.Number),
  replyToUserId: Schema.optional(Schema.Number),
  callbackId: Schema.optional(Schema.String),
});

export const ActionPayloadSchema = Schema.Struct({
  reason: Schema.optional(Schema.String),
  messageChatId: Schema.optional(Schema.Number),
  followup: Schema.optional(Schema.Boolean),
  text: Schema.optional(Schema.String),
  sentChatId: Schema.optional(Schema.Number),
  receipt: Schema.optional(Schema.Number),
  followupCreatedAt: Schema.optional(Schema.Number),
  banActionId: Schema.optional(Schema.String),
});

export function decode<T>(value: string, schema: Schema.Codec<T>): T {
  return Schema.decodeUnknownSync(schema)(JSON.parse(value));
}
