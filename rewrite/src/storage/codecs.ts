import { Schema } from 'effect'

export const encode = (value: unknown): string => JSON.stringify(value)
export const ObjectSchema = Schema.Record(Schema.String, Schema.Unknown)

export const MediaSchema = Schema.Array(
  Schema.Struct({
    kind: Schema.Literals(['photo', 'document', 'video', 'unsupported']),
    fileId: Schema.optionalKey(Schema.String),
    blobHash: Schema.optionalKey(Schema.String),
    mimeType: Schema.optionalKey(Schema.String),
    size: Schema.optionalKey(Schema.Number),
  }),
)

export const ContextMessageSchema = Schema.Struct({
  messageId: Schema.Number,
  userId: Schema.optionalKey(Schema.Number),
  sameAuthor: Schema.Boolean,
  isReplyParent: Schema.Boolean,
  sender: Schema.optionalKey(Schema.String),
  occurredAt: Schema.Number,
  text: Schema.String,
})

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
  context: Schema.optionalKey(Schema.Array(ContextMessageSchema)),
  contextTruncated: Schema.optionalKey(Schema.Boolean),
})

export const ClassificationSchema = Schema.Struct({
  verdict: Schema.Literals(['spam', 'clean', 'uncertain']),
  reason: Schema.String,
  evidenceMessageIds: Schema.Array(Schema.Number),
  model: Schema.String,
  promptVersion: Schema.String,
  usage: Schema.optionalKey(Schema.Record(Schema.String, Schema.Number)),
})

export const CommandSchema = Schema.Struct({
  id: Schema.String,
  chatId: Schema.Number,
  userId: Schema.Number,
  text: Schema.String,
  messageId: Schema.optionalKey(Schema.Number),
  replyToMessageId: Schema.optionalKey(Schema.Number),
  replyToUserId: Schema.optionalKey(Schema.Number),
  callbackId: Schema.optionalKey(Schema.String),
})

export const ActionPayloadSchema = Schema.Struct({
  reason: Schema.optionalKey(Schema.String),
  messageChatId: Schema.optionalKey(Schema.Number),
  followup: Schema.optionalKey(Schema.Boolean),
  text: Schema.optionalKey(Schema.String),
  sentChatId: Schema.optionalKey(Schema.Number),
  receipt: Schema.optionalKey(Schema.Number),
  followupCreatedAt: Schema.optionalKey(Schema.Number),
  banActionId: Schema.optionalKey(Schema.String),
})

export function decode<T>(value: string, schema: Schema.Codec<T>): T {
  return Schema.decodeUnknownSync(schema)(JSON.parse(value))
}
