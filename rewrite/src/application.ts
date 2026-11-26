import { Effect } from "effect";
import { LanguageModel } from "effect/ai";
import { anthropicLayer } from "./classification/anthropic.ts";
import { SpamClassifier } from "./classifier.ts";
import { failure, type Settings } from "./domain.ts";
import { Engine } from "./engine.ts";
import { acquireProcessLock } from "./process-lock.ts";
import { Store } from "./store.ts";
import { createTelegram } from "./telegram.ts";
import { startHealthServer } from "./runtime/health.ts";
import { ObservationIngress } from "./runtime/ingress.ts";
import { RuntimeLog } from "./runtime/log.ts";
import {
  deliverAdminAlerts,
  exportJournal,
  observeMessages,
  processCommands,
  processModeration,
  receiveUpdates,
  runRetentionLoop,
  snapshotStatus,
  type MutableRuntimeStatus,
  type RuntimeServices,
} from "./runtime/workers.ts";

/** Own resources once; stop every network loop before closing SQLite and releasing the lock. */
export function application(settings: Settings) {
  const provider = settings.provider ?? "anthropic";
  const program = Effect.scoped(
    Effect.gen(function* () {
      const log = new RuntimeLog(settings);
      const status: MutableRuntimeStatus = {
        startedAt: Date.now(),
        botId: 0,
        botUsername: "",
        ready: false,
        lastPollAt: 0,
        observerConfigured: Boolean(settings.observer),
        observerConnected: false,
        observerLastEventAt: 0,
        observerDisconnectedSince: settings.observer ? Date.now() : null,
      };
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
      const healthServer = yield* Effect.acquireRelease(
        Effect.try({
          try: () => startHealthServer(settings, store, () => snapshotStatus(status)),
          catch: failure,
        }),
        (server) => Effect.sync(() => server?.close()),
      );
      if (healthServer) {
        log.event("health_server_listening", {
          host: settings.healthHost ?? "127.0.0.1",
          port: healthServer.port,
        });
      }

      const { port: telegram, api } = yield* Effect.tryPromise({
        try: (signal) => createTelegram(settings, signal),
        catch: failure,
      }).pipe(Effect.timeout("30 seconds"));
      status.botId = telegram.botId;
      status.botUsername = telegram.botUsername;

      const classifier =
        provider === "anthropic"
          ? yield* LanguageModel.LanguageModel.pipe(
              Effect.map((model) => new SpamClassifier(settings, api, model)),
              Effect.provide(anthropicLayer(settings)),
            )
          : new SpamClassifier(settings, api);

      const engine = yield* Effect.try({
        try: () => new Engine(store, classifier, telegram, settings),
        catch: failure,
      });
      const services: RuntimeServices = { settings, store, api, telegram, log, status };
      const ingress = new ObservationIngress(store, telegram.botId);
      yield* checkTelegramAccess(services);
      status.ready = true;

      log.event("started", {
        mode: settings.mode,
        provider,
        model: settings.model,
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
          deliverAdminAlerts(services),
          runRetentionLoop(store, log),
          observeMessages(services, ingress),
        ],
        { concurrency: "unbounded", discard: true },
      );
    }),
  );

  return program;
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
              const canDelete =
                member.status === "creator" ||
                (member.status === "administrator" && member.can_delete_messages);
              log.event("chat_permissions", { chatId, status: member.status, canBan, canDelete });
              const name = settings.chatNames?.[String(chatId)] ?? `Чат ${chatId}`;
              store.recordIncident(
                `chat_perms:${chatId}`,
                !canBan || !canDelete,
                !canBan || !canDelete
                  ? `Недостаточно прав в чате ${name}: ban=${String(canBan)}, delete=${String(canDelete)}`
                  : `Права в чате ${name} восстановлены`,
              );
            }),
          ),
          Effect.catch((error) =>
            Effect.sync(() => {
              log.error("chat_permission_check_failed", error, { chatId });
              const name = settings.chatNames?.[String(chatId)] ?? `Чат ${chatId}`;
              store.recordIncident(
                `chat_perms:${chatId}`,
                true,
                `Не удалось проверить права бота в чате ${name}`,
              );
            }),
          ),
        ),
      { concurrency: 1, discard: true },
    );
  });
}
