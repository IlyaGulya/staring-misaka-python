import { Effect } from "effect";
import type { Api } from "grammy";
import { handleCommand } from "../commands.ts";
import { failure, type RuntimeStatus, type Settings, type TelegramPort } from "../domain.ts";
import { Engine } from "../engine.ts";
import { runObserver } from "../observer.ts";
import { Store } from "../store.ts";
import { runAdminAlertCycle } from "./alerts.ts";
import { ObservationIngress } from "./ingress.ts";
import { RuntimeLog } from "./log.ts";

export interface MutableRuntimeStatus {
  readonly startedAt: number;
  botId: number;
  botUsername: string;
  ready: boolean;
  lastPollAt: number;
  readonly observerConfigured: boolean;
  observerConnected: boolean;
  observerLastEventAt: number;
  observerDisconnectedSince: number | null;
}

export interface RuntimeServices {
  readonly settings: Settings;
  readonly store: Store;
  readonly api: Api;
  readonly telegram: TelegramPort;
  readonly log: RuntimeLog;
  readonly status: MutableRuntimeStatus;
}

export function snapshotStatus(status: MutableRuntimeStatus): RuntimeStatus {
  return { ...status };
}

export function receiveUpdates(services: RuntimeServices, ingress: ObservationIngress) {
  const { settings, store, api, log, status } = services;
  return Effect.gen(function* () {
    const updates = yield* Effect.tryPromise({
      try: (signal) =>
        api.getUpdates(
          {
            offset: store.getOffset(),
            timeout: settings.pollTimeout,
            allowed_updates: [
              "message",
              "edited_message",
              "channel_post",
              "edited_channel_post",
              "chat_member",
              "my_chat_member",
              "callback_query",
            ],
          },
          signal,
        ),
      catch: failure,
    });
    status.lastPollAt = Date.now();
    yield* Effect.try({ try: () => ingress.acceptUpdates(updates), catch: failure });
  }).pipe(
    Effect.catch((error) =>
      Effect.gen(function* () {
        log.error("receiver_failed", error);
        yield* Effect.sleep(settings.retryBaseMs);
      }),
    ),
    Effect.forever,
  );
}

export function processModeration(engine: Engine, log: RuntimeLog) {
  return Effect.gen(function* () {
    yield* engine
      .tick()
      .pipe(Effect.catch((error) => Effect.sync(() => log.error("worker_failed", error))));
    yield* Effect.sleep(200);
  }).pipe(Effect.forever);
}

export function processCommands(services: RuntimeServices) {
  const { settings, store, api, telegram, log, status } = services;
  return Effect.gen(function* () {
    const command = yield* Effect.try({ try: () => store.claimCommand(), catch: failure });
    if (command) {
      yield* Effect.tryPromise({
        try: (signal) =>
          handleCommand(command, store, telegram, api, settings, signal, snapshotStatus(status)),
        catch: failure,
      }).pipe(
        Effect.tap(() =>
          Effect.try({ try: () => store.finishCommand(command.id), catch: failure }),
        ),
        Effect.catch((error) =>
          Effect.try({
            try: () => {
              const message = log.error("command_failed", error, { id: command.id });
              store.failCommand(command.id, message);
            },
            catch: failure,
          }),
        ),
      );
    }
    yield* Effect.sleep(200);
  }).pipe(Effect.forever);
}

export function exportJournal(store: Store, log: RuntimeLog) {
  return Effect.gen(function* () {
    yield* Effect.try({ try: () => store.exportJournal(), catch: failure }).pipe(
      Effect.catch((error) => Effect.sync(() => log.error("journal_export_failed", error))),
    );
    yield* Effect.sleep(1000);
  }).pipe(Effect.forever);
}

export function deliverAdminAlerts(services: RuntimeServices) {
  const { settings, store, api, log, status } = services;
  return Effect.gen(function* () {
    yield* Effect.tryPromise({
      try: (signal) =>
        runAdminAlertCycle(store, api, settings, snapshotStatus(status), log, signal),
      catch: failure,
    }).pipe(
      Effect.catch((error) => Effect.sync(() => log.error("admin_alert_cycle_failed", error))),
    );
    yield* Effect.sleep(5000);
  }).pipe(Effect.forever);
}

export function runRetentionLoop(store: Store, log: RuntimeLog) {
  if ((store.settings.retentionDays ?? 30) <= 0) return Effect.never;
  const sixHours = 6 * 60 * 60 * 1000;
  return Effect.gen(function* () {
    // Wait briefly after startup before the first maintenance pass.
    yield* Effect.sleep(5 * 60 * 1000);
    yield* Effect.gen(function* () {
      yield* Effect.try({
        try: () => {
          const report = store.runRetention();
          if (report.prunedObservations + report.prunedAudit + report.prunedMedia > 0) {
            log.event("retention_completed", { ...report });
          }
        },
        catch: failure,
      }).pipe(Effect.catch((error) => Effect.sync(() => log.error("retention_failed", error))));
      yield* Effect.sleep(sixHours);
    }).pipe(Effect.forever);
  });
}

export function observeMessages(services: RuntimeServices, ingress: ObservationIngress) {
  const { settings, store, log, status } = services;
  if (!settings.observer) return Effect.never;

  return Effect.gen(function* () {
    log.event("observer_connecting");
    yield* Effect.tryPromise({
      try: (signal) =>
        runObserver(
          settings,
          (observation) => {
            status.observerLastEventAt = Date.now();
            ingress.observe(observation);
          },
          signal,
          (chatId) => store.tracksChat(chatId),
          () => {
            status.observerConnected = true;
            status.observerDisconnectedSince = null;
            log.event("observer_connected");
          },
        ),
      catch: failure,
    }).pipe(
      Effect.catch((error) =>
        Effect.sync(() => {
          status.observerConnected = false;
          status.observerDisconnectedSince ??= Date.now();
          log.error("observer_disconnected", error);
        }),
      ),
    );
    status.observerConnected = false;
    status.observerDisconnectedSince ??= Date.now();
    yield* Effect.sleep(settings.retryBaseMs);
  }).pipe(Effect.forever);
}
