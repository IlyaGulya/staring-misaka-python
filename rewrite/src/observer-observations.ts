import { createHash } from 'node:crypto'
import type { MediaRef, Observation } from './domain'
import { isCommandText } from './command-text'

type RuntimeObject = Record<string, unknown>

function isObject(value: unknown): value is RuntimeObject {
  return typeof value === 'object' && value !== null
}

function object(value: unknown): RuntimeObject {
  return isObject(value) ? value : {}
}

// GramJS IDs may be numbers, native bigints, or big-integer instances.
function numericText(value: unknown): string | undefined {
  if (typeof value === 'number' || typeof value === 'bigint' || typeof value === 'string') {
    return String(value)
  }
  if (!isObject(value)) return undefined
  const format: unknown = Reflect.get(value, 'toString')
  if (typeof format !== 'function' || format === Object.prototype.toString) return undefined
  const text: unknown = format.call(value)
  return typeof text === 'string' && /^-?\d+$/.test(text) ? text : undefined
}

function integer(value: unknown): number | undefined {
  const result = Number(numericText(value))
  return Number.isSafeInteger(result) ? result : undefined
}

function peerId(value: unknown): number | undefined {
  const peer = object(value)
  if (peer.channelId !== undefined) {
    const id = integer(peer.channelId)
    return id === undefined ? undefined : -1_000_000_000_000 - id
  }
  if (peer.chatId !== undefined) {
    const id = integer(peer.chatId)
    return id === undefined ? undefined : -id
  }
  return integer(peer.userId)
}

function jsonData(value: unknown): unknown {
  if (value === undefined) return null
  return JSON.parse(
    JSON.stringify(value, (key, item: unknown) => {
      if (key.startsWith('_')) return undefined
      return typeof item === 'bigint' ? String(item) : item
    }),
  )
}

interface MessageContext {
  message: RuntimeObject
  base: Pick<Observation, 'source' | 'sourceKey' | 'chatId' | 'messageId' | 'occurredAt' | 'raw'>
  userId: number | undefined
  editDate: number | undefined
}

function messageContext(value: unknown): MessageContext | undefined {
  const message = object(value)
  const chatId = peerId(message.peerId)
  const messageId = integer(message.id)
  const date = integer(message.date)
  if (chatId === undefined || messageId === undefined || date === undefined) return undefined

  const editDate = integer(message.editDate)
  const senderId = integer(object(message.fromId).userId)
  const userId = senderId ?? (chatId > 0 && message.out !== true ? chatId : undefined)
  // Telegram revision timestamps have second precision. Hash content to preserve same-second edits.
  const content = {
    text: message.message ?? '',
    entities: jsonData(message.entities),
    media: jsonData(message.media),
    buttons: jsonData(message.replyMarkup),
    forward: jsonData(message.fwdFrom),
    action: jsonData(message.action),
    reply: jsonData(message.replyTo),
  }
  const contentHash = createHash('sha256')
    .update(JSON.stringify(content))
    .digest('hex')
    .slice(0, 24)
  return {
    message,
    userId,
    editDate,
    base: {
      source: 'observer',
      sourceKey: `observer:${chatId}:${messageId}:${editDate ?? date}:${contentHash}`,
      chatId,
      messageId,
      occurredAt: date * 1000,
      raw: jsonData(value),
    },
  }
}

function serviceObservations({ message, base, userId }: MessageContext): Observation[] {
  const action = object(message.action)
  switch (action.className) {
    case 'MessageActionChatAddUser': {
      if (!Array.isArray(action.users)) return []
      const observations: Observation[] = []
      for (const [index, id] of action.users.entries()) {
        const addedUserId = integer(id)
        observations.push({
          ...base,
          sourceKey: `${base.sourceKey}:join:${index}`,
          kind: 'membership',
          ...(addedUserId === undefined ? {} : { userId: addedUserId }),
          actorKind: 'user',
          membership: 'joined',
          ...(userId === undefined ? {} : { changedBy: userId }),
        })
      }
      return observations
    }
    case 'MessageActionChatJoinedByLink':
    case 'MessageActionChatJoinedByRequest': {
      const inviterId = integer(action.inviterId)
      return [
        {
          ...base,
          kind: 'membership',
          ...(userId === undefined ? {} : { userId }),
          actorKind: 'user',
          membership: 'joined',
          ...(inviterId === undefined ? {} : { changedBy: inviterId }),
        },
      ]
    }
    case 'MessageActionChatDeleteUser': {
      const removedUserId = integer(action.userId)
      return [
        {
          ...base,
          kind: 'membership',
          ...(removedUserId === undefined ? {} : { userId: removedUserId }),
          actorKind: 'user',
          membership: 'left',
          ...(userId === undefined ? {} : { changedBy: userId }),
        },
      ]
    }
    case 'MessageActionChatMigrateTo': {
      const migratedTo = peerId({ channelId: action.channelId })
      return [
        {
          ...base,
          kind: 'migration',
          ...(migratedTo === undefined ? {} : { migratedTo }),
        },
      ]
    }
    case 'MessageActionChannelMigrateFrom':
      return [
        {
          ...base,
          kind: 'migration',
          chatId: peerId({ chatId: action.chatId }) ?? base.chatId,
          migratedTo: base.chatId,
        },
      ]
    default:
      return []
  }
}

function messageMedia(value: unknown): MediaRef[] {
  const attachment = object(value)
  switch (attachment.className) {
    case 'MessageMediaPhoto':
      return [{ kind: 'photo', mimeType: 'image/jpeg' }]
    case 'MessageMediaDocument': {
      const document = object(attachment.document)
      const mimeType = typeof document.mimeType === 'string' ? document.mimeType : undefined
      const size = integer(document.size)
      return [
        {
          kind: 'document',
          ...(mimeType === undefined ? {} : { mimeType }),
          ...(size === undefined ? {} : { size }),
        },
      ]
    }
    case 'MessageMediaEmpty':
    case 'MessageMediaWebPage':
      return []
    default:
      return value ? [{ kind: 'unsupported' }] : []
  }
}

function messageEntities(value: unknown): RuntimeObject[] {
  if (!Array.isArray(value)) return []
  const entities: RuntimeObject[] = []
  for (const item of value) {
    const entity = object(item)
    const className = typeof entity.className === 'string' ? entity.className : ''
    const type = className
      .replace(/^MessageEntity/, '')
      .replace(/[A-Z]/g, (letter, index: number) => `${index ? '_' : ''}${letter.toLowerCase()}`)
    const record: RuntimeObject = {
      ...object(jsonData(item)),
      type,
      offset: entity.offset,
      length: entity.length,
    }
    if (entity.userId !== undefined) {
      record.userId = integer(entity.userId)
    }
    entities.push(record)
  }
  return entities
}

function messageButtons(value: unknown): RuntimeObject[][] {
  const rows = object(value).rows
  if (!Array.isArray(rows)) return []
  const result: RuntimeObject[][] = []
  for (const row of rows) {
    const buttons = object(row).buttons
    if (!Array.isArray(buttons)) {
      result.push([])
      continue
    }
    const rowButtons: RuntimeObject[] = []
    for (const button of buttons) {
      const item = object(button)
      const record: RuntimeObject = {
        ...object(jsonData(button)),
        text: item.text,
        url: item.url,
      }
      if (item.data instanceof Uint8Array) {
        record.callback_data = Buffer.from(item.data).toString('utf8')
      }
      rowButtons.push(record)
    }
    result.push(rowButtons)
  }
  return result
}

function messageMetadata(
  message: RuntimeObject,
  text: string,
): NonNullable<Observation['metadata']> {
  const sender = object(message.sender)
  const reply = object(message.replyTo)
  const replyPeer = object(reply.replyToPeerId)
  const externalChatId = peerId(replyPeer)
  let chatType = 'private'
  if (replyPeer.channelId !== undefined) chatType = 'channel_peer'
  else if (replyPeer.chatId !== undefined) chatType = 'group'

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
            text: typeof reply.quoteText === 'string' ? reply.quoteText : undefined,
          },
    entities: messageEntities(message.entities),
    buttons: messageButtons(message.replyMarkup),
    forward: jsonData(message.fwdFrom),
    replyToMessageId: reply.replyToMsgId ?? null,
    mediaGroupId: numericText(message.groupedId) ?? null,
    media: jsonData(message.media),
    senderPeer: jsonData(message.fromId),
  }
}

/** Uses Telegram's revision clock, never receipt time. Does not download or mutate remote state. */
export function normalizeObserverMessage(value: unknown, edited = false): Observation[] {
  const context = messageContext(value)
  if (!context) return []
  const { message, base, userId, editDate } = context
  if (message.action) return serviceObservations(context)

  const sender = object(message.sender)
  const text = typeof message.message === 'string' ? message.message : ''
  let actorKind: Observation['actorKind'] = 'unknown'
  if (userId !== undefined) actorKind = 'user'
  else if (object(message.fromId).channelId !== undefined || message.post === true)
    actorKind = 'channel'

  const username = typeof sender.username === 'string' ? sender.username : undefined
  return [
    {
      ...base,
      kind: edited ? 'edit' : 'message',
      ...(userId === undefined ? {} : { userId }),
      actorKind,
      ...(username === undefined ? {} : { username }),
      displayName: [sender.firstName, sender.lastName].filter(Boolean).join(' '),
      text,
      ...(editDate === undefined ? {} : { editedAt: editDate * 1000 }),
      media: messageMedia(message.media),
      metadata: messageMetadata(message, text),
    },
  ]
}

interface RawObservations {
  observations: Observation[]
  unscopedDeletions: number
}

function deletionObservations(
  update: RuntimeObject,
  nonChannelMessages: ReadonlyMap<number, number>,
  now: number,
): RawObservations {
  if (!Array.isArray(update.messages)) return { observations: [], unscopedDeletions: 0 }
  const byChat = new Map<number, number[]>()
  let unscopedDeletions = 0
  for (const id of update.messages) {
    const messageId = integer(id)
    if (messageId === undefined) continue
    const chatId =
      update.channelId === undefined
        ? nonChannelMessages.get(messageId)
        : peerId({ channelId: update.channelId })
    // Telegram omits chat identity for private/basic-group deletions. Never guess.
    if (chatId === undefined) {
      unscopedDeletions++
      continue
    }
    const ids = byChat.get(chatId) ?? []
    ids.push(messageId)
    byChat.set(chatId, ids)
  }
  const observations: Observation[] = []
  for (const [chatId, deletedIds] of byChat) {
    observations.push({
      source: 'observer',
      sourceKey: `observer:delete:${chatId}:${String(update.pts)}:${deletedIds.join(',')}`,
      kind: 'delete',
      chatId,
      occurredAt: now,
      deletedIds,
      raw: jsonData(update),
    })
  }
  return { observations, unscopedDeletions }
}

function participantObservations(update: RuntimeObject, now: number): Observation[] {
  const chatId =
    update.channelId === undefined
      ? peerId({ chatId: update.chatId })
      : peerId({ channelId: update.channelId })
  if (chatId === undefined) return []

  const participant = object(update.newParticipant)
  const previous = object(update.prevParticipant)
  const banned = object(participant.bannedRights).viewMessages === true
  const previouslyBanned = object(previous.bannedRights).viewMessages === true
  const left =
    update.className === 'UpdateChatParticipantDelete' ||
    participant.left === true ||
    (update.className !== 'UpdateChatParticipantAdd' && !update.newParticipant)
  let membership: Observation['membership'] = 'joined'
  if (banned) membership = 'banned'
  else if (left) membership = previouslyBanned ? 'unbanned' : 'left'

  const participantUserId = integer(update.userId)
  const changedBy = integer(update.actorId ?? update.inviterId)
  return [
    {
      source: 'observer',
      sourceKey: `observer:membership:${chatId}:${String(update.userId)}:${String(update.qts ?? update.version ?? update.date)}`,
      kind: 'membership',
      chatId,
      ...(participantUserId === undefined ? {} : { userId: participantUserId }),
      actorKind: 'user',
      occurredAt: (integer(update.date) ?? Math.floor(now / 1000)) * 1000,
      ...(changedBy === undefined ? {} : { changedBy }),
      membership,
      raw: jsonData(update),
    },
  ]
}

/** Normalizes raw service/deletion/participant updates independently of the client lifecycle. */
export function normalizeObserverUpdate(
  value: unknown,
  nonChannelMessages: ReadonlyMap<number, number>,
  now: number,
): RawObservations {
  const update = object(value)
  switch (update.className) {
    case 'UpdateNewMessage':
    case 'UpdateNewChannelMessage':
      return {
        observations: object(update.message).action ? normalizeObserverMessage(update.message) : [],
        unscopedDeletions: 0,
      }
    case 'UpdateDeleteChannelMessages':
    case 'UpdateDeleteMessages':
      return deletionObservations(update, nonChannelMessages, now)
    case 'UpdateChannelParticipant':
    case 'UpdateChatParticipant':
    case 'UpdateChatParticipantAdd':
    case 'UpdateChatParticipantDelete':
      return { observations: participantObservations(update, now), unscopedDeletions: 0 }
    default:
      return { observations: [], unscopedDeletions: 0 }
  }
}
