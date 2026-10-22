import { Effect } from "effect";
import {
  AppError,
  failure,
  type ActionJob,
  type ClassificationJob,
  type ClassifierPort,
  type Settings,
  type TelegramPort,
} from "./domain.ts";
import { notificationText } from "./notifications.ts";
import { Store } from "./store.ts";

/** A bounded batch. The application owns polling and the surrounding cancellation scope. */
export class Engine {
  private readonly slots: readonly number[];

  constructor(
    private readonly store: Store,
    private readonly classifier: ClassifierPort,
    private readonly telegram: TelegramPort,
    private readonly settings: Settings,
  ) {
    this.slots = Array.from({ length: settings.concurrency }, (_, index) => index);
    store.bindBot(telegram.botId, telegram.botUsername);
  }

  tick(): Effect.Effect<void, AppError> {
    return Effect.gen({ self: this }, function* () {
      const classificationErrors = yield* Effect.forEach(this.slots, () => this.classifyNext(), {
        concurrency: this.settings.concurrency,
      });

      // Accepted actions run even when classification failed or chat monitoring is disabled.
      const actionErrors = yield* Effect.forEach(this.slots, () => this.executeNext(), {
        concurrency: this.settings.concurrency,
      });
      const error =
        classificationErrors.find((error) => error !== undefined) ??
        actionErrors.find((error) => error !== undefined);
      if (error) yield* Effect.fail(error);
    });
  }

  private classifyNext(): Effect.Effect<AppError | undefined, AppError> {
    return Effect.acquireUseRelease(
      Effect.try({ try: () => this.store.claimClassification(), catch: failure }),
      (job) => (job ? this.classify(job) : Effect.succeed(undefined)),
      (job) =>
        job
          ? Effect.try({ try: () => this.store.interruptClassification(job), catch: failure })
          : Effect.void,
    );
  }

  private classify(job: ClassificationJob): Effect.Effect<AppError | undefined, AppError> {
    const operation = Effect.gen({ self: this }, function* () {
      const result = yield* this.classifier.classify(job.snapshot);
      yield* Effect.try({
        try: () => this.store.completeClassification(job, result),
        catch: failure,
      });
    }).pipe(
      Effect.timeoutOrElse({
        duration: this.settings.classificationTimeoutMs,
        orElse: () =>
          Effect.fail(new AppError({ category: "transient", message: "Classification timed out" })),
      }),
      Effect.withSpan("moderation.classify", {
        attributes: { jobId: job.id, snapshotId: job.snapshot.id },
      }),
    );

    return Effect.matchEffect(operation, {
      onSuccess: () => Effect.succeed(undefined),
      onFailure: (error) =>
        Effect.try({
          try: () => {
            this.store.failClassification(job, error);
            return error;
          },
          catch: failure,
        }),
    });
  }

  private executeNext(): Effect.Effect<AppError | undefined, AppError> {
    return Effect.acquireUseRelease(
      Effect.try({ try: () => this.store.claimAction(), catch: failure }),
      (action) => (action ? this.execute(action) : Effect.succeed(undefined)),
      (action) =>
        action
          ? Effect.try({ try: () => this.store.interruptAction(action), catch: failure })
          : Effect.void,
    );
  }

  private execute(action: ActionJob): Effect.Effect<AppError | undefined, AppError> {
    const operation = Effect.gen({ self: this }, function* () {
      const targetChatId = this.store.resolveChatId(action.chatId);
      const changesMembership = action.kind === "ban" || action.kind === "unban";
      const membership = changesMembership
        ? yield* Effect.tryPromise({
            try: (signal) => this.telegram.membership(targetChatId, action.userId, signal),
            catch: failure,
          })
        : undefined;

      // Recheck token, generation and ownership after awaiting Telegram, before dispatch.
      const allowed = yield* Effect.try({
        try: () => this.store.prepareAction(action, membership, targetChatId),
        catch: failure,
      });
      if (!allowed) return;

      const routedAction: ActionJob = {
        ...action,
        chatId: targetChatId,
        payload: { ...action.payload, sentChatId: targetChatId },
      };
      const receipt = yield* Effect.tryPromise({
        try: (signal) => executeAction(routedAction, this.telegram, signal),
        catch: failure,
      });
      yield* Effect.try({
        try: () => this.store.completeAction(routedAction, receipt),
        catch: failure,
      });
    }).pipe(
      Effect.timeoutOrElse({
        duration: 30_000,
        orElse: () =>
          Effect.fail(
            new AppError({
              category: "unknown",
              message: "Telegram action timed out; reconciliation required",
            }),
          ),
      }),
      Effect.withSpan("moderation.action", {
        attributes: { actionId: action.id, decisionId: action.decisionId, kind: action.kind },
      }),
    );

    return Effect.matchEffect(operation, {
      onSuccess: () => Effect.succeed(undefined),
      onFailure: (error) =>
        Effect.try({
          try: () => {
            this.store.failAction(action, error);
            return error;
          },
          catch: failure,
        }),
    });
  }
}

async function executeAction(
  action: ActionJob,
  telegram: TelegramPort,
  signal: AbortSignal,
): Promise<number | undefined> {
  switch (action.kind) {
    case "ban":
      await telegram.ban(action.chatId, action.userId, signal);
      return undefined;
    case "unban":
      await telegram.unban(action.chatId, action.userId, signal);
      return undefined;
    case "delete": {
      if (action.messageId === undefined) {
        throw new AppError({ category: "invalid", message: "Delete action has no message ID" });
      }
      const messageChatId = action.payload.messageChatId ?? action.chatId;
      await telegram.delete(messageChatId, action.messageId, signal);
      return undefined;
    }
    case "edit": {
      if (action.messageId === undefined || action.payload.text === undefined) {
        throw new AppError({
          category: "invalid",
          message: "Edit action has no message ID or text",
        });
      }
      const messageChatId = action.payload.messageChatId ?? action.chatId;
      await telegram.edit(messageChatId, action.messageId, action.payload.text, signal);
      return undefined;
    }
    case "notify":
      return telegram.notify(
        action.chatId,
        notificationText(telegram.botUsername, action.decisionId),
        signal,
      );
  }
}
