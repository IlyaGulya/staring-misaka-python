import { afterAll, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { Effect, Fiber } from "effect";
import { LanguageModel } from "effect/ai";
import { anthropicLayer } from "../src/classification/anthropic";
import { Api, GrammyError } from "grammy";
import type { ChatFullInfo, Update } from "grammy/types";
import { mkdtemp, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import bigInt from "big-integer";
import { Api as MTProto, TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions";
import { SpamClassifier } from "../src/classifier";
import { enrichObserverMedia } from "../src/observer";
import { normalizeObserverMessage } from "../src/observer-observations";
import { commandFromUpdate, normalizeUpdate } from "../src/telegram-observations";
import { telegramError } from "../src/telegram";
import type { Settings, Snapshot } from "../src/domain";
import { loadImage, MAX_IMAGE_BYTES, persistImage } from "../src/media";
import { withTelemetry } from "../src/telemetry";

const settings: Settings = {
  mode: "shadow",
  dbPath: ":memory:",
  botToken: "test:secret",
  adminId: 1,
  chatIds: [-1000000000123],
  anthropicKey: "test-key",
  anthropicUrl: "https://anthropic.invalid",
  model: "claude-sonnet-4-5-20250929",
  systemPrompt: "Detect unsolicited advertising",
  chatPrompts: { "-1000000000123": "Custom chat policy" },
  telegramApiRoot: "https://telegram.invalid",
  pollTimeout: 25,
  concurrency: 1,
  followupTtlMs: 60_000,
  classificationTimeoutMs: 30_000,
  maxClassificationAttempts: 3,
  maxActionAttempts: 5,
  retryBaseMs: 1000,
  recoveryIntervalMs: 30_000,
  journalPath: "/dev/null",
};
const snapshot: Snapshot = {
  id: "snapshot",
  chatId: settings.chatIds[0]!,
  userId: 10,
  generation: 0,
  version: 0,
  messages: [{ messageId: 4, revision: 1, text: "hello", media: [], metadata: {} }],
};
const fullChat: ChatFullInfo.SupergroupChat = {
  id: snapshot.chatId,
  type: "supergroup",
  title: "chat",
  accent_color_id: 0,
  max_reaction_count: 1,
  accepted_gift_types: {
    unlimited_gifts: false,
    limited_gifts: false,
    unique_gifts: false,
    premium_subscription: false,
    gifts_from_channels: false,
  },
};

function evidenceSnapshot(message: Partial<Snapshot["messages"][number]>): Snapshot {
  return { ...snapshot, messages: [{ ...snapshot.messages[0]!, ...message }] };
}

const externalSnapshot = evidenceSnapshot({
  metadata: {
    externalReply: {
      chatId: -1000000000456,
      chatType: "channel",
      messageId: 99,
      text: "quoted channel post",
    },
  },
});

function botMessage<const Fields extends Partial<NonNullable<Update["message"]>>>(fields: Fields) {
  return {
    message_id: 4,
    date: 100,
    chat: { id: snapshot.chatId, type: "supergroup" as const, title: "chat" },
    from: { id: 10, is_bot: false, first_name: "Sender" },
    ...fields,
  };
}

function observerMessage(fields: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 4,
    date: 100,
    peerId: { channelId: 123n },
    fromId: { userId: 10n },
    sender: { firstName: "Sender" },
    ...fields,
  };
}

function report(
  verdict: string,
  ids: readonly number[] = [4],
  options: {
    reason?: string;
    stopReason?: string;
    usage?: Record<string, unknown>;
    toolReport?: boolean;
  } = {},
): Response {
  const finding = {
    verdict,
    reason: options.reason ?? "Evidence-based finding",
    evidenceMessageIds: ids,
  };
  return Response.json({
    id: "msg_test",
    type: "message",
    role: "assistant",
    model: "resolved-model",
    stop_reason: options.stopReason ?? (options.toolReport ? "tool_use" : "end_turn"),
    stop_sequence: null,
    content: options.toolReport
      ? [{ type: "tool_use", id: "tool_report", name: "report_classification", input: finding }]
      : [{ type: "text", text: JSON.stringify(finding) }],
    usage: {
      input_tokens: 12,
      output_tokens: 8,
      cache_creation: null,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
      service_tier: "standard",
      ...options.usage,
    },
  });
}

function classification(
  evidence: Snapshot,
  api = new Api(settings.botToken),
  configuration = settings,
) {
  return Effect.gen(function* () {
    const model = yield* LanguageModel.LanguageModel;
    return yield* new SpamClassifier(configuration, api, model).classify(evidence);
  }).pipe(Effect.provide(anthropicLayer(configuration)));
}

function classify(evidence: Snapshot, api?: Api, configuration?: Settings) {
  return Effect.runPromise(classification(evidence, api, configuration));
}

function requestText(index: number): string {
  const body = fetchSpy.mock.calls[index]?.[1]?.body;
  if (typeof body === "string") return body;
  if (body instanceof Uint8Array) return new TextDecoder().decode(body);
  throw new Error("Expected a serialized HTTP request body");
}

const nativeFetch = globalThis.fetch;
const fetchSpy = spyOn(globalThis, "fetch");
// Requests are intercepted or explicitly restricted to a test's loopback server.
beforeEach(() => {
  fetchSpy.mockReset();
  fetchSpy.mockRejectedValue(new Error("Unexpected network request"));
});
afterAll(() => {
  fetchSpy.mockRestore();
});

describe("Telegram normalization", () => {
  test("Bot and observer use the same semantic revision clock and retain bot actors", () => {
    const bot = normalizeUpdate({
      update_id: 1,
      edited_message: botMessage({
        edit_date: 120,
        from: { id: 10, is_bot: true, first_name: "bot" },
        text: "edited",
      }),
    })[0]!;
    const observer = normalizeObserverMessage(
      observerMessage({
        editDate: 120,
        sender: { bot: true },
        message: "edited",
      }),
      true,
    )[0]!;
    expect({
      chatId: observer.chatId,
      messageId: observer.messageId,
      userId: observer.userId,
      occurredAt: observer.occurredAt,
      editedAt: observer.editedAt,
      text: observer.text,
    }).toEqual({
      chatId: bot.chatId,
      messageId: bot.messageId,
      userId: bot.userId,
      occurredAt: bot.occurredAt,
      editedAt: bot.editedAt,
      text: bot.text,
    });
    expect(bot.actorKind).toBe("user");
    expect(observer.actorKind).toBe("user");
    expect(bot.metadata?.isBot).toBe(true);
    expect(observer.metadata?.isBot).toBe(true);
  });

  test("commands remain archived separately and preserve reply target", () => {
    const update: Update = {
      update_id: 2,
      message: botMessage({
        message_id: 5,
        from: { id: 1, is_bot: false, first_name: "admin" },
        text: "/approve",
        reply_to_message: {
          message_id: 4,
          date: 99,
          chat: { id: snapshot.chatId, type: "supergroup", title: "chat" },
          from: { id: 10, is_bot: true, first_name: "bot actor" },
          text: "hello",
          reply_to_message: undefined,
        },
      }),
    };
    expect(commandFromUpdate(update)).toMatchObject({
      replyToMessageId: 4,
      replyToUserId: 10,
      userId: 1,
    });
    expect(normalizeUpdate(update)[0]?.metadata?.isCommand).toBe(true);
  });

  test("channel senders are not bannable user actors", () => {
    const update: Update = {
      update_id: 3,
      message: botMessage({
        sender_chat: { id: -1000000000456, type: "channel", title: "channel" },
        from: { id: 777, is_bot: true, first_name: "anonymous" },
        text: "post",
      }),
    };
    expect(normalizeUpdate(update)[0]).toMatchObject({ actorKind: "channel", userId: undefined });
  });

  test("join service messages enroll every member without classifying service text", () => {
    const update: Update = {
      update_id: 4,
      message: botMessage({
        message_id: 6,
        from: { id: 1, is_bot: false, first_name: "admin" },
        new_chat_members: [
          { id: 10, is_bot: true, first_name: "bot" },
          { id: 11, is_bot: false, first_name: "user" },
        ],
      }),
    };
    expect(normalizeUpdate(update).map((item) => [item.kind, item.userId])).toEqual([
      ["membership", 10],
      ["membership", 11],
    ]);
  });

  test("observer migrations match Bot API marked channel IDs", () => {
    const message = observerMessage({
      id: 7,
      peerId: { chatId: 123n },
      action: { className: "MessageActionChatMigrateTo", channelId: 456n },
    });
    expect(normalizeObserverMessage(message)[0]).toMatchObject({
      kind: "migration",
      chatId: -123,
      migratedTo: -1000000000456,
    });
  });

  test("both transports preserve external reply context and available names", () => {
    const update: Update = {
      update_id: 8,
      message: botMessage({
        text: "reply",
        external_reply: {
          origin: {
            type: "channel",
            date: 99,
            chat: { id: -1000000000456, type: "channel", title: "other" },
            message_id: 99,
          },
        },
        quote: { text: "quoted channel post", position: 0 },
      }),
    };
    const bot = normalizeUpdate(update)[0]!;
    const observer = normalizeObserverMessage(
      observerMessage({
        message: "reply",
        replyTo: {
          replyToPeerId: { channelId: 456n },
          replyToMsgId: 99,
          quoteText: "quoted channel post",
        },
      }),
    )[0]!;
    expect(bot.metadata?.externalReply).toEqual({
      chatId: -1000000000456,
      chatType: "channel",
      messageId: 99,
      text: "quoted channel post",
    });
    expect(observer.metadata?.externalReply).toEqual({
      chatId: -1000000000456,
      chatType: "channel_peer",
      messageId: 99,
      text: "quoted channel post",
    });
    expect(bot.metadata?.sender).toMatchObject({ firstName: "Sender" });
    expect(observer.metadata?.sender).toMatchObject({ firstName: "Sender" });
  });

  test("distinct same-second observer edits survive source deduplication", () => {
    const message = observerMessage({ editDate: 120, message: "first revision" });
    const first = normalizeObserverMessage(message, true)[0]!;
    const duplicate = normalizeObserverMessage({ ...message }, true)[0]!;
    const changed = normalizeObserverMessage({ ...message, message: "second revision" }, true)[0]!;
    expect(first.sourceKey).toBe(duplicate.sourceKey);
    expect(first.sourceKey).not.toBe(changed.sourceKey);
    expect(first.editedAt).toBe(changed.editedAt);
  });

  test("undo callback requires an accessible message chat", () => {
    const callback = {
      id: "callback",
      from: { id: 1, is_bot: false as const, first_name: "admin" },
      chat_instance: "chat",
      data: "undo:decision",
    };
    expect(commandFromUpdate({ update_id: 5, callback_query: callback })).toBeUndefined();
    const command = commandFromUpdate({
      update_id: 6,
      callback_query: {
        ...callback,
        message: botMessage({
          message_id: 9,
          chat: { id: -123, type: "group", title: "chat" },
          text: "notice",
        }),
      },
    });
    expect(command).toMatchObject({ callbackId: "callback", text: "undo:decision", chatId: -123 });
  });

  test("lost mutation responses remain unknown and redact credentials", () => {
    const error = telegramError(
      new Error("https://telegram.invalid/bottest:secret/banChatMember"),
      true,
    );
    expect(error.category).toBe("unknown");
    expect(error.message).not.toContain("secret");
    expect(telegramError(new Error("offline")).category).toBe("transient");
    const response = new GrammyError(
      "rate limit",
      {
        ok: false,
        error_code: 429,
        description: "rate limit",
        parameters: { retry_after: 12 },
      },
      "banChatMember",
      {},
    );
    const retry = telegramError(response, true);
    expect(retry.category).toBe("transient");
    expect(retry.retryAfterMs).toBe(12_000);
  });
});

describe("Anthropic structured classification", () => {
  test("policy provenance follows the selected chat policy, not unrelated chat changes", async () => {
    fetchSpy.mockImplementation(
      Object.assign(async () => report("clean"), { preconnect: fetch.preconnect }),
    );
    const original = await classify(snapshot);
    const unrelated = await classify(snapshot, undefined, {
      ...settings,
      systemPrompt: "Different global policy",
    });
    const changed = await classify(snapshot, undefined, {
      ...settings,
      chatPrompts: { [snapshot.chatId]: "Different chat policy" },
    });
    expect(unrelated.promptVersion).toBe(original.promptVersion);
    expect(changed.promptVersion).not.toBe(original.promptVersion);
  });

  test("accepts provider usage metadata without discarding a valid classification", async () => {
    fetchSpy.mockResolvedValueOnce(
      report("spam", [4], {
        usage: {
          input_tokens: 3120,
          output_tokens: 180,
          cache_creation_input_tokens: 200,
          cache_read_input_tokens: 900,
          cache_creation: { ephemeral_5m_input_tokens: 200, ephemeral_1h_input_tokens: 0 },
          service_tier: "standard",
          inference_geo: "not_available",
          speed: "standard",
        },
      }),
    );
    const result = await classify(snapshot);
    expect(result.verdict).toBe("spam");
    expect(result.evidenceMessageIds).toEqual([4]);
    expect(result.model).toBe("resolved-model");
    expect(result.usage).toEqual({
      input_tokens: 3120,
      output_tokens: 180,
      cache_creation_input_tokens: 200,
      cache_read_input_tokens: 900,
    });
  });

  test("accepts SDK structured reports for configured models without native JSON output", async () => {
    fetchSpy.mockResolvedValueOnce(report("clean", [4], { toolReport: true }));
    const result = await classify(snapshot, undefined, {
      ...settings,
      model: "claude-3-7-sonnet-20250219",
    });
    expect(result.verdict).toBe("clean");
    expect(result.evidenceMessageIds).toEqual([4]);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  test("unrelated channel replies become rule decisions without invoking the model", async () => {
    const api = new Api(settings.botToken);
    const chat = spyOn(api, "getChat").mockResolvedValue(fullChat);
    try {
      const result = await classify(externalSnapshot, api);
      expect(result).toMatchObject({
        verdict: "spam",
        evidenceMessageIds: [4],
        model: "rule/cross-channel-reply",
        promptVersion: "cross-channel-reply-v1",
      });
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      chat.mockRestore();
    }
  });

  test("linked-channel replies are exempt from the rule and remain ordinary LLM evidence", async () => {
    const api = new Api(settings.botToken);
    const chat = spyOn(api, "getChat").mockResolvedValue({
      ...fullChat,
      linked_chat_id: -1000000000456,
    });
    fetchSpy.mockResolvedValueOnce(report("clean"));
    try {
      const result = await classify(externalSnapshot, api);
      expect(result).toMatchObject({ verdict: "clean", model: "resolved-model" });
    } finally {
      chat.mockRestore();
    }
  });

  test("unknown linked-channel state falls back to the model with reply context", async () => {
    const api = new Api(settings.botToken);
    const chat = spyOn(api, "getChat").mockRejectedValue(new Error("Telegram unavailable"));
    fetchSpy.mockResolvedValueOnce(report("uncertain"));
    try {
      const result = await classify(externalSnapshot, api);
      expect(result).toMatchObject({ verdict: "uncertain", model: "resolved-model" });
      expect(requestText(0)).toContain("quoted channel post");
    } finally {
      chat.mockRestore();
    }
  });

  test("same-chat replies do not trigger the unrelated-channel rule", async () => {
    const api = new Api(settings.botToken);
    const chat = spyOn(api, "getChat").mockRejectedValue(new Error("Unexpected Telegram request"));
    const evidence = evidenceSnapshot({
      metadata: { externalReply: { chatId: snapshot.chatId, chatType: "channel_peer" } },
    });
    fetchSpy.mockResolvedValueOnce(report("clean"));
    try {
      const result = await classify(evidence, api);
      expect(result.verdict).toBe("clean");
      expect(chat).not.toHaveBeenCalled();
    } finally {
      chat.mockRestore();
    }
  });

  test("rejects invented evidence IDs and schema-invalid verdicts", async () => {
    fetchSpy.mockResolvedValueOnce(report("spam", [999]));
    await expect(classify(snapshot)).rejects.toMatchObject({ category: "invalid" });
    fetchSpy.mockResolvedValueOnce(report("approve"));
    await expect(classify(snapshot)).rejects.toMatchObject({ category: "invalid" });
  });

  test("observer-only photos cannot become fake clean evidence", async () => {
    fetchSpy.mockResolvedValueOnce(report("clean"));
    const evidence = evidenceSnapshot({ media: [{ kind: "photo", mimeType: "image/jpeg" }] });
    const result = await classify(evidence);
    expect(result.verdict).toBe("uncertain");
  });

  test("store-truncated evidence cannot become a clean verdict", async () => {
    fetchSpy.mockResolvedValueOnce(report("clean"));
    const evidence = evidenceSnapshot({ metadata: { evidenceTruncated: true } });
    const result = await classify(evidence);
    expect(result.verdict).toBe("uncertain");
  });

  test("downloads supported images through getFile and labels their message evidence", async () => {
    const api = new Api(settings.botToken);
    const file = spyOn(api, "getFile").mockResolvedValue({
      file_id: "image",
      file_unique_id: "unique",
      file_path: "photos/image.jpg",
      file_size: 4,
    });
    const evidence = evidenceSnapshot({
      media: [{ kind: "photo", fileId: "image", mimeType: "image/jpeg" }],
    });
    fetchSpy
      .mockResolvedValueOnce(new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xd9])))
      .mockResolvedValueOnce(report("clean"));
    try {
      const result = await classify(evidence, api);
      expect(result.verdict).toBe("clean");
      expect(file.mock.calls[0]?.[0]).toBe("image");
      const request = JSON.parse(requestText(1));
      const image = request.messages[0].content.find(
        (part: { type: string }) => part.type === "image",
      );
      expect(image).toMatchObject({
        type: "image",
        source: { type: "base64", media_type: "image/jpeg", data: "/9j/2Q==" },
      });
    } finally {
      file.mockRestore();
    }
  });

  test("oversized download headers prevent missing images from becoming clean", async () => {
    const api = new Api(settings.botToken);
    const file = spyOn(api, "getFile").mockResolvedValue({
      file_id: "image",
      file_unique_id: "unique",
      file_path: "photos/image.jpg",
    });
    const evidence = evidenceSnapshot({
      media: [{ kind: "photo", fileId: "image", mimeType: "image/jpeg" }],
    });
    fetchSpy
      .mockResolvedValueOnce(
        new Response("too large", { headers: { "content-length": String(6 * 1024 * 1024) } }),
      )
      .mockResolvedValueOnce(report("clean"));
    try {
      const result = await classify(evidence, api);
      expect(result.verdict).toBe("uncertain");
    } finally {
      file.mockRestore();
    }
  });

  test.each(["max_tokens", "refusal", "pause_turn"])(
    "rejects %s even when the response contains a valid clean object",
    async (stopReason) => {
      fetchSpy.mockResolvedValueOnce(report("clean", [4], { stopReason }));
      await expect(classify(snapshot)).rejects.toMatchObject({ category: "invalid" });
    },
  );

  test("rate limits retain retry guidance without response-body secrets", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response("sensitive upstream detail", {
        status: 429,
        headers: { "retry-after": "2" },
      }),
    );
    await expect(classify(snapshot)).rejects.toMatchObject({
      category: "transient",
      retryAfterMs: 2000,
    });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  test.each([
    { ids: [], reason: "spam without evidence" },
    { ids: [4.5], reason: "fractional message ID" },
    { ids: [4], reason: "   " },
    { ids: [4], reason: "x".repeat(4001) },
  ])("rejects unsupported or invalid reports %#", async ({ ids, reason }) => {
    fetchSpy.mockResolvedValueOnce(report("spam", ids, { reason }));
    await expect(classify(snapshot)).rejects.toMatchObject({ category: "invalid" });
  });

  test("deduplicates valid evidence references", async () => {
    fetchSpy.mockResolvedValueOnce(report("spam", [4, 4]));
    expect((await classify(snapshot)).evidenceMessageIds).toEqual([4]);
  });

  test.each([
    { status: 401, category: "permanent" },
    { status: 503, category: "transient" },
    { status: 408, category: "transient" },
  ])(
    "returns HTTP $status to the durable queue without SDK retries",
    async ({ status, category }) => {
      fetchSpy.mockResolvedValueOnce(
        new Response("sensitive upstream detail test-key", {
          status,
          headers: { "retry-after": "2" },
        }),
      );
      const outcome = await Effect.runPromise(classification(snapshot).pipe(Effect.result));
      expect(outcome._tag).toBe("Failure");
      if (outcome._tag !== "Failure") throw new Error("Expected a provider failure");
      expect(outcome.failure).toMatchObject({ category, retryAfterMs: 2000 });
      expect(JSON.stringify(outcome.failure)).not.toContain("sensitive upstream detail");
      expect(JSON.stringify(outcome.failure)).not.toContain("test-key");
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    },
  );

  test.each([undefined, "1"])(
    "bounds decoded response bytes with Content-Length %s",
    async (declared) => {
      const body = await report("clean").text();
      fetchSpy.mockResolvedValueOnce(
        new Response(body + " ".repeat(256 * 1024), {
          headers: declared === undefined ? {} : { "content-length": declared },
        }),
      );
      await expect(classify(snapshot)).rejects.toMatchObject({ category: "invalid" });
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    },
  );

  test("Effect interruption aborts the native HTTP request", async () => {
    const started = Promise.withResolvers<void>();
    let aborted = false;
    fetchSpy.mockImplementation(
      Object.assign(
        (_input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) =>
          new Promise<Response>((_resolve, reject) => {
            const signal = init?.signal;
            if (!signal) throw new Error("Missing HTTP cancellation signal");
            signal.addEventListener(
              "abort",
              () => {
                aborted = true;
                reject(signal.reason);
              },
              { once: true },
            );
            started.resolve();
          }),
        { preconnect: fetch.preconnect },
      ),
    );
    const fiber = Effect.runFork(classification(snapshot));
    await started.promise;
    await Effect.runPromise(Fiber.interrupt(fiber));
    expect(aborted).toBe(true);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  test.each(["redirect", "oversized"] as const)(
    "provider %s protection survives an independently configured telemetry client",
    async (mode) => {
      let redirected = false;
      const oversized = await report("clean").text();
      const server = Bun.serve({
        hostname: "127.0.0.1",
        port: 0,
        fetch(request) {
          const path = new URL(request.url).pathname;
          if (path === "/traces") return Response.json({});
          if (path === "/redirect-target") {
            redirected = true;
            return report("clean");
          }
          if (path !== "/v1/messages") throw new Error("Unexpected loopback route");
          return mode === "redirect"
            ? Response.redirect(new URL("/redirect-target", request.url), 307)
            : new Response(oversized + " ".repeat(256 * 1024));
        },
      });
      fetchSpy.mockImplementation(
        Object.assign(
          (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
            const url = new URL(input instanceof Request ? input.url : String(input));
            if (url.origin !== server.url.origin) throw new Error("Non-loopback network request");
            return nativeFetch(input, init);
          },
          { preconnect: nativeFetch.preconnect },
        ),
      );
      try {
        const program = classification(snapshot, undefined, {
          ...settings,
          anthropicUrl: server.url.origin,
        });
        const outcome = await Effect.runPromise(
          withTelemetry(program, `${server.url.origin}/traces`).pipe(Effect.result),
        );
        expect(outcome._tag).toBe("Failure");
        if (outcome._tag !== "Failure") throw new Error("Expected a rejected provider response");
        expect(outcome.failure.category).toBe(mode === "redirect" ? "transient" : "invalid");
        expect(redirected).toBe(false);
      } finally {
        server.stop(true);
      }
    },
  );
});

describe("Managed observer image evidence", () => {
  test("persists exclusive private blobs and classifies observer-only images", async () => {
    const directory = await mkdtemp(join(tmpdir(), "misaka-media-"));
    const localSettings = { ...settings, dbPath: join(directory, "state.sqlite") };
    const bytes = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
    try {
      const [hash, duplicate] = await Promise.all([
        persistImage(localSettings.dbPath, bytes),
        persistImage(localSettings.dbPath, bytes),
      ]);
      expect(hash).toBe(duplicate);
      expect(hash).toMatch(/^[a-f0-9]{64}$/);
      expect((await stat(join(directory, "media", hash))).mode & 0o777).toBe(0o600);
      expect((await loadImage(localSettings.dbPath, hash)).mime).toBe("image/jpeg");
      fetchSpy.mockResolvedValueOnce(report("clean"));
      const evidence = evidenceSnapshot({
        media: [{ kind: "photo", blobHash: hash, mimeType: "image/jpeg" }],
      });
      const result = await classify(evidence, undefined, localSettings);
      expect(result.verdict).toBe("clean");
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  test("rejects path-shaped hashes, corrupted blobs and unsupported data", async () => {
    const directory = await mkdtemp(join(tmpdir(), "misaka-media-"));
    const dbPath = join(directory, "state.sqlite");
    try {
      await expect(loadImage(dbPath, "../../secret")).rejects.toMatchObject({
        category: "invalid",
      });
      await expect(persistImage(dbPath, Buffer.from("not an image"))).rejects.toMatchObject({
        category: "invalid",
      });
      const hash = await persistImage(dbPath, Buffer.from([0xff, 0xd8, 0xff, 0xd9]));
      await writeFile(join(directory, "media", hash), Buffer.from([0xff, 0xd8, 0xff, 0x00]));
      await expect(loadImage(dbPath, hash)).rejects.toMatchObject({ category: "invalid" });
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  test("observer download limits abort progress and cancellation is not swallowed", async () => {
    const client = new TelegramClient(new StringSession(""), 123, "test-hash", {});
    const message = new MTProto.Message({
      id: 4,
      date: 100,
      message: "",
      peerId: new MTProto.PeerChannel({ channelId: bigInt(123) }),
      fromId: new MTProto.PeerUser({ userId: bigInt(10) }),
      media: new MTProto.MessageMediaPhoto({}),
    });
    const observation = normalizeObserverMessage(message)[0]!;
    const download = spyOn(client, "downloadMedia").mockImplementation(
      async (_message, options) => {
        options?.progressCallback?.(bigInt(MAX_IMAGE_BYTES + 1), bigInt(MAX_IMAGE_BYTES + 1));
        throw new Error("Progress callback must reject oversized images");
      },
    );
    try {
      const result = await enrichObserverMedia(
        client,
        message,
        observation,
        settings,
        new AbortController().signal,
      );
      expect(result.metadata?.mediaUnavailable).toBe(true);
      expect(result.media?.[0]?.blobHash).toBeUndefined();
      const abort = new AbortController();
      download.mockImplementation(async () => {
        abort.abort();
        throw new Error("download cancelled");
      });
      await expect(
        enrichObserverMedia(client, message, observation, settings, abort.signal),
      ).rejects.toBeDefined();
    } finally {
      download.mockRestore();
    }
  });
});
