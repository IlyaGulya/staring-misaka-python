import { Data, type Effect } from "effect";

export class AppError extends Data.TaggedError("AppError")<{
  readonly message: string;
  readonly category: "transient" | "permanent" | "unknown" | "invalid";
  readonly retryAfterMs?: number;
}> {}

export type Mode = "shadow" | "live";
export type Verdict = "spam" | "clean" | "uncertain";
export type ActorKind = "user" | "channel" | "unknown";
export interface MediaRef {
  readonly kind: "photo" | "document" | "video" | "unsupported";
  readonly fileId?: string;
  readonly blobHash?: string;
  readonly mimeType?: string;
  readonly size?: number;
}
export interface Observation {
  readonly source: "bot" | "observer" | "replay";
  readonly sourceKey: string;
  readonly kind: "message" | "edit" | "delete" | "membership" | "migration";
  readonly chatId: number;
  readonly messageId?: number;
  readonly userId?: number;
  readonly actorKind?: ActorKind;
  readonly username?: string;
  readonly displayName?: string;
  readonly text?: string;
  readonly occurredAt: number;
  readonly editedAt?: number;
  readonly media?: ReadonlyArray<MediaRef>;
  readonly metadata?: Record<string, unknown>;
  readonly deletedIds?: ReadonlyArray<number>;
  readonly membership?: "joined" | "left" | "banned" | "unbanned";
  readonly changedBy?: number;
  readonly migratedTo?: number;
  readonly raw?: unknown;
}
export interface Snapshot {
  readonly id: string;
  readonly chatId: number;
  readonly userId: number;
  readonly generation: number;
  readonly version: number;
  readonly messages: ReadonlyArray<{
    readonly messageId: number;
    readonly revision: number;
    readonly text: string;
    readonly media: ReadonlyArray<MediaRef>;
    readonly metadata: Record<string, unknown>;
  }>;
}
export interface Classification {
  readonly verdict: Verdict;
  readonly reason: string;
  readonly evidenceMessageIds: ReadonlyArray<number>;
  readonly model: string;
  readonly promptVersion: string;
  readonly usage?: Record<string, number>;
}
export interface Decision extends Classification {
  readonly id: string;
  readonly chatId: number;
  readonly userId: number;
  readonly snapshotId: string;
  readonly createdAt: number;
  readonly overriddenAt: number | null;
  readonly status: string;
}

export interface Command {
  readonly id: string;
  readonly chatId: number;
  readonly userId: number;
  readonly text: string;
  readonly messageId?: number;
  readonly replyToMessageId?: number;
  readonly replyToUserId?: number;
  readonly callbackId?: string;
}

export interface ClassificationJob {
  readonly id: string;
  readonly token: string;
  readonly attempt: number;
  readonly snapshot: Snapshot;
}

export interface ActionJob extends Action {
  readonly phase: "preflight" | "reconcile";
  readonly undoRequested: boolean;
}
export interface Action {
  readonly id: string;
  readonly kind: "ban" | "unban" | "delete" | "notify" | "edit";
  readonly decisionId: string;
  readonly chatId: number;
  readonly userId: number;
  readonly generation: number;
  readonly messageId?: number;
  readonly state: ActionState;
  readonly attempt: number;
  readonly token: string;
  readonly payload: ActionPayload;
}

export type ActionState =
  | "pending"
  | "retry"
  | "blocked"
  | "shadow"
  | "preflight"
  | "sending"
  | "reconciling"
  | "unknown"
  | "needs_attention"
  | "confirmed"
  | "cancelled";

/** Persisted action intent and optional delivery receipt; field names are stable on disk. */
export interface ActionPayload {
  readonly reason?: string;
  readonly messageChatId?: number;
  readonly followup?: boolean;
  readonly text?: string;
  readonly sentChatId?: number;
  readonly receipt?: number;
  readonly followupCreatedAt?: number;
  readonly banActionId?: string;
}

export interface ActionAttempt {
  readonly attempt: number;
  readonly startedAt: number;
  readonly finishedAt: number | null;
  readonly outcome: string | null;
  readonly error: string | null;
}

export interface ActionDetail extends Omit<Action, "messageId"> {
  readonly messageId: number | null;
  readonly messageChatId: number | null;
  readonly mode: Mode;
  readonly retryAt: number;
  readonly error: string | null;
  readonly undoRequested: number;
  readonly owned: number;
  readonly ownershipEpoch: number | null;
  readonly preflightEpoch: number | null;
  readonly sentAt: number | null;
  readonly confirmedAt: number | null;
  readonly createdAt: number;
  readonly attempts: ReadonlyArray<ActionAttempt>;
}

export interface StoreHealth {
  readonly mode: Mode;
  readonly offset: number;
  readonly journalCursor: number;
  readonly [counter: string]: number | Mode;
}
export interface Membership {
  readonly restricted: boolean;
  readonly isMember: boolean;
}
export interface TelegramPort {
  readonly botId: number;
  readonly botUsername: string;
  ban(chatId: number, userId: number, signal: AbortSignal): Promise<void>;
  unban(chatId: number, userId: number, signal: AbortSignal): Promise<void>;
  membership(chatId: number, userId: number, signal: AbortSignal): Promise<Membership>;
  delete(chatId: number, messageId: number, signal: AbortSignal): Promise<void>;
  notify(chatId: number, text: string, signal: AbortSignal): Promise<number>;
  edit(chatId: number, messageId: number, text: string, signal: AbortSignal): Promise<void>;
  isAdmin(chatId: number, userId: number, signal: AbortSignal): Promise<boolean>;
}
export interface ClassifierPort {
  classify(snapshot: Snapshot): Effect.Effect<Classification, AppError>;
}
export interface Settings {
  readonly mode: Mode;
  readonly dbPath: string;
  readonly botToken: string;
  readonly adminId: number;
  readonly chatIds: ReadonlyArray<number>;
  readonly anthropicKey: string;
  readonly anthropicUrl: string;
  readonly model: string;
  readonly systemPrompt: string;
  readonly chatPrompts: Readonly<Record<string, string>>;
  readonly telegramApiRoot: string;
  readonly pollTimeout: number;
  readonly concurrency: number;
  readonly followupTtlMs: number;
  readonly classificationTimeoutMs: number;
  readonly maxClassificationAttempts: number;
  readonly maxActionAttempts: number;
  readonly retryBaseMs: number;
  readonly recoveryIntervalMs: number;
  readonly journalPath: string;
  readonly observer?: {
    readonly apiId: number;
    readonly apiHash: string;
    readonly session: string;
  };
}
export function failure(error: unknown): AppError {
  if (error instanceof AppError) return error;
  return new AppError({
    message: error instanceof Error ? error.message : String(error),
    category: "transient",
  });
}
