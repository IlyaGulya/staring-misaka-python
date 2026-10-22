import { Effect } from "effect";
import type { Api } from "grammy";
import { handleCommand } from "../commands.ts";
import { failure, type Settings, type TelegramPort } from "../domain.ts";
import { Engine } from "../engine.ts";
import { runObserver } from "../observer.ts";
import { Store } from "../store.ts";
import { ObservationIngress } from "./ingress.ts";
import { RuntimeLog } from "./log.ts";

export interface RuntimeServices {
  readonly settings: Settings;
  readonly store: Store;
  readonly api: Api;
  readonly telegram: TelegramPort;
  readonly log: RuntimeLog;
}

export function receiveUpdates(services: RuntimeServices, ingress: ObservationIngress) {
  const { settings, store, api, log } = services;
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
  const { settings, store, api, telegram, log } = services;
  return Effect.gen(function* () {
    const command = yield* Effect.try({ try: () => store.claimCommand(), catch: failure });
    if (command) {
      yield* Effect.tryPromise({
        try: (signal) => handleCommand(command, store, telegram, api, settings, signal),
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

export function observeMessages(services: RuntimeServices, ingress: ObservationIngress) {
  const { settings, store, log } = services;
  if (!settings.observer) return Effect.never;

  return Effect.gen(function* () {
    log.event("observer_connecting");
    yield* Effect.tryPromise({
      try: (signal) =>
        runObserver(
          settings,
          (observation) => ingress.observe(observation),
          signal,
          (chatId) => store.tracksChat(chatId),
        ),
      catch: failure,
    }).pipe(Effect.catch((error) => Effect.sync(() => log.error("observer_disconnected", error))));
    yield* Effect.sleep(settings.retryBaseMs);
  }).pipe(Effect.forever);
}
