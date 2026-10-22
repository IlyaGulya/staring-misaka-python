import { TelegramClient, type Api as TelegramApi } from "telegram";
import { StringSession } from "telegram/sessions";
import { NewMessage, Raw } from "telegram/events";
import { EditedMessage } from "telegram/events/EditedMessage";
import { UpdateConnectionState } from "telegram/network";
import { AppError, type Observation, type Settings } from "./domain";
import { imageMime, MAX_IMAGE_BYTES, persistImage } from "./media";
import { normalizeObserverMessage, normalizeObserverUpdate } from "./observer-observations";

const MAX_PENDING_MESSAGES = 64;
const MAX_TRACKED_NON_CHANNEL_MESSAGES = 100_000;
const supportedImageTypes: Record<string, true> = {
  "image/jpeg": true,
  "image/png": true,
  "image/gif": true,
  "image/webp": true,
};

function unavailableMedia(observation: Observation): Observation {
  return {
    ...observation,
    metadata: { ...observation.metadata, mediaUnavailable: true },
  };
}

/** Retrieves evidence read-only and publishes blob references only after durable persistence. */
export async function enrichObserverMedia(
  client: TelegramClient,
  message: TelegramApi.Message,
  observation: Observation,
  settings: Settings,
  signal: AbortSignal,
): Promise<Observation> {
  const media = observation.media?.[0];
  if (!media || (media.kind !== "photo" && media.kind !== "document")) return observation;
  if ((media.size ?? 0) > MAX_IMAGE_BYTES) return observation;
  if (
    media.kind === "document" &&
    (!media.mimeType || supportedImageTypes[media.mimeType] !== true)
  ) {
    return observation;
  }

  signal.throwIfAborted();
  let bytes: Buffer | string | undefined;
  try {
    bytes = await client.downloadMedia(message, {
      progressCallback(received, total) {
        signal.throwIfAborted();
        if (
          Number(received.toString()) > MAX_IMAGE_BYTES ||
          Number(total.toString()) > MAX_IMAGE_BYTES
        ) {
          throw new AppError({
            message: "Observer image exceeds download limit",
            category: "invalid",
          });
        }
      },
    });
  } catch {
    signal.throwIfAborted();
    return unavailableMedia(observation);
  }
  signal.throwIfAborted();
  if (!Buffer.isBuffer(bytes) || bytes.byteLength > MAX_IMAGE_BYTES) {
    return unavailableMedia(observation);
  }
  const mime = imageMime(bytes);
  if (!mime || (media.mimeType && media.mimeType !== mime)) {
    return unavailableMedia(observation);
  }

  const blobHash = await persistImage(settings.dbPath, bytes);
  signal.throwIfAborted();
  return {
    ...observation,
    media: [
      { ...media, blobHash, mimeType: mime, size: bytes.byteLength },
      ...(observation.media?.slice(1) ?? []),
    ],
  };
}

export async function runObserver(
  settings: Settings,
  onObservation: (observation: Observation) => void,
  signal: AbortSignal,
  shouldDownload: (chatId: number) => boolean = (chatId) => settings.chatIds.includes(chatId),
): Promise<void> {
  const observer = settings.observer;
  if (!observer) {
    throw new AppError({ message: "Observer session is not configured", category: "invalid" });
  }
  if (signal.aborted) return;

  const client = new TelegramClient(
    new StringSession(observer.session),
    observer.apiId,
    observer.apiHash,
    { connectionRetries: 3, requestRetries: 2, autoReconnect: false },
  );
  const { promise: lifetime, resolve: finish, reject: fail } = Promise.withResolvers<void>();
  let stopping = false;
  let ready = false;
  let messageQueue = Promise.resolve();
  let pendingMessages = 0;
  const nonChannelMessages = new Map<number, number>();

  function publish(observation: Observation): void {
    if (stopping) return;
    try {
      onObservation(observation);
      if (observation.messageId !== undefined && observation.chatId > -1_000_000_000_000) {
        nonChannelMessages.set(observation.messageId, observation.chatId);
        if (nonChannelMessages.size > MAX_TRACKED_NON_CHANNEL_MESSAGES) {
          const oldest = nonChannelMessages.keys().next().value;
          if (oldest !== undefined) nonChannelMessages.delete(oldest);
        }
      }
    } catch {
      fail(new AppError({ message: "Observer durable ingestion failed", category: "permanent" }));
    }
  }

  async function receive(message: TelegramApi.Message, edited: boolean): Promise<void> {
    if (stopping) return;
    if (pendingMessages >= MAX_PENDING_MESSAGES) {
      fail(
        new AppError({
          message: "Observer media ingestion queue overflow; update gap possible",
          category: "transient",
        }),
      );
      return;
    }
    pendingMessages++;
    messageQueue = messageQueue
      .then(async () => {
        if (stopping) return;
        for (const observation of normalizeObserverMessage(message, edited)) {
          const enriched = shouldDownload(observation.chatId)
            ? await enrichObserverMedia(client, message, observation, settings, signal)
            : observation;
          publish(enriched);
        }
      })
      .catch(() => {
        if (!stopping && !signal.aborted) {
          fail(
            new AppError({ message: "Observer evidence ingestion failed", category: "transient" }),
          );
        }
      })
      .finally(() => {
        pendingMessages--;
      });
    await messageQueue;
  }

  function receiveRaw(value: unknown): void {
    if (value instanceof UpdateConnectionState) {
      if (ready && !stopping && value.state !== UpdateConnectionState.connected) {
        fail(
          new AppError({
            message: "Observer disconnected; restart required, update gap possible",
            category: "transient",
          }),
        );
      }
      return;
    }
    try {
      const { observations, unscopedDeletions } = normalizeObserverUpdate(
        value,
        nonChannelMessages,
        Date.now(),
      );
      for (let index = 0; index < unscopedDeletions; index++) {
        console.error("Observer received unscoped deletion; chat cannot be recovered");
      }
      for (const observation of observations) publish(observation);
    } catch {
      fail(
        new AppError({ message: "Observer metadata normalization failed", category: "invalid" }),
      );
    }
  }

  // Register before connecting so incoming updates cannot race durable ingestion.
  // Errors must reach lifetime: GramJS otherwise swallows event callback exceptions.
  client.onError = async () => {
    if (!stopping) {
      fail(
        new AppError({
          message: "Observer connection or update processing failed",
          category: "transient",
        }),
      );
    }
  };
  client.addEventHandler((event) => receive(event.message, false), new NewMessage({}));
  client.addEventHandler((event) => receive(event.message, true), new EditedMessage({}));
  client.addEventHandler(receiveRaw, new Raw({}));

  function abort(): void {
    stopping = true;
    finish();
    void client.disconnect().catch(() => undefined);
  }
  signal.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(() => {
    fail(new AppError({ message: "Observer startup timed out", category: "transient" }));
  }, 45_000);

  const startup = (async () => {
    await client.connect();
    if (stopping) return;
    if (!(await client.checkAuthorization())) {
      throw new AppError({
        message: "Observer session unauthorized; run explicit observer login",
        category: "permanent",
      });
    }
    if ((await client.getMe()).bot) {
      throw new AppError({
        message: "Observer requires a user session, not a bot",
        category: "permanent",
      });
    }
    if (!client.connected) {
      throw new AppError({
        message: "Observer disconnected during startup",
        category: "transient",
      });
    }
    ready = true;
    clearTimeout(timeout);
    await lifetime;
  })();

  try {
    await Promise.race([startup, lifetime]);
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw new AppError({ message: "Observer startup failed", category: "transient" });
  } finally {
    stopping = true;
    clearTimeout(timeout);
    signal.removeEventListener("abort", abort);
    await client.destroy();
    // A late connection completion must not leave a client alive after cancellation.
    void startup.finally(() => client.destroy()).catch(() => undefined);
  }
}
