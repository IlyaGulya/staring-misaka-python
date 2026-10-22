import { Effect } from "effect";
import { LanguageModel } from "effect/ai";
import { anthropicLayer } from "./classification/anthropic.ts";
import { SpamClassifier } from "./classifier.ts";
import { failure, type Settings } from "./domain.ts";
import { Engine } from "./engine.ts";
import { acquireProcessLock } from "./process-lock.ts";
import { Store } from "./store.ts";
import { createTelegram } from "./telegram.ts";
import { ObservationIngress } from "./runtime/ingress.ts";
import { RuntimeLog } from "./runtime/log.ts";
import {
  exportJournal,
  observeMessages,
  processCommands,
  processModeration,
  receiveUpdates,
  type RuntimeServices,
} from "./runtime/workers.ts";

/** Own resources once; stop every network loop before closing SQLite and releasing the lock. */
export function application(settings: Settings) {
  return Effect.scoped(
    Effect.gen(function* () {
      const log = new RuntimeLog(settings);
      yield* Effect.acquireRelease(
        Effect.try({ try: () => acquireProcessLock(settings.dbPath), catch: failure }),
        (release) => Effect.sync(release),
      );
      const store = yield* Effect.acquireRelease(
        Effect.try({ try: () => new Store(settings), catch: failure }),
        (store) =>
          Effect.sync(() => {
            try {
              store.exportJournal();
            } catch (error) {
              log.error("journal_export_failed", error);
            }
            store.close();
            log.event("stopped");
          }),
      );
      const { port: telegram, api } = yield* Effect.tryPromise({
        try: (signal) => createTelegram(settings, signal),
        catch: failure,
      }).pipe(Effect.timeout("30 seconds"));

      const model = yield* LanguageModel.LanguageModel;
      const classifier = new SpamClassifier(settings, api, model);
      const engine = yield* Effect.try({
        try: () => new Engine(store, classifier, telegram, settings),
        catch: failure,
      });
      const services: RuntimeServices = { settings, store, api, telegram, log };
      const ingress = new ObservationIngress(store, telegram.botId);
      yield* checkTelegramAccess(services);

      log.event("started", {
        mode: settings.mode,
        botId: telegram.botId,
        chats: settings.chatIds,
        observer: Boolean(settings.observer),
      });
      if (!settings.observer) {
        log.event("observer_coverage_gap", {
          reason: "Observer not configured; only Bot API deliveries are covered",
        });
      }

      yield* Effect.all(
        [
          receiveUpdates(services, ingress),
          processModeration(engine, log),
          processCommands(services),
          exportJournal(store, log),
          observeMessages(services, ingress),
        ],
        { concurrency: "unbounded", discard: true },
      );
    }),
  ).pipe(Effect.provide(anthropicLayer(settings)));
}

function checkTelegramAccess(services: RuntimeServices) {
  const { settings, store, api, telegram, log } = services;
  return Effect.gen(function* () {
    const webhook = yield* Effect.tryPromise({
      try: (signal) => api.getWebhookInfo(signal),
      catch: failure,
    });
    if (webhook.url) {
      return yield* Effect.fail(
        failure(
          new Error(
            "Webhook configured; refusing to replace another receiver. Remove it explicitly before starting polling.",
          ),
        ),
      );
    }

    yield* Effect.forEach(
      settings.chatIds,
      (chatId) =>
        Effect.tryPromise({
          try: (signal) => api.getChatMember(store.resolveChatId(chatId), telegram.botId, signal),
          catch: failure,
        }).pipe(
          Effect.tap((member) =>
            Effect.sync(() => {
              const canBan =
                member.status === "creator" ||
                (member.status === "administrator" && member.can_restrict_members);
              log.event("chat_permissions", { chatId, status: member.status, canBan });
            }),
          ),
          Effect.catch((error) =>
            Effect.sync(() => {
              log.error("chat_permission_check_failed", error, { chatId });
            }),
          ),
        ),
      { concurrency: 1, discard: true },
    );
  });
}
