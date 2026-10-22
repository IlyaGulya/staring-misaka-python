import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { Effect, Fiber } from "effect";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  AppError,
  type Classification,
  type ClassificationJob,
  type ClassifierPort,
  type Membership,
  type Observation,
  type Settings,
  type Snapshot,
  type TelegramPort,
} from "../src/domain";
import { Engine } from "../src/engine";
import { Store } from "../src/store";

const stores = new Set<Store>();
const directories: string[] = [];
afterEach(() => {
  for (const store of stores) store.close();
  stores.clear();
  for (const directory of directories.splice(0))
    rmSync(directory, { recursive: true, force: true });
});

function configuration(overrides: Partial<Settings> = {}): Settings {
  const directory = mkdtempSync(join(tmpdir(), "misaka-core-"));
  directories.push(directory);
  return {
    mode: "live",
    dbPath: join(directory, "store.sqlite"),
    journalPath: join(directory, "journal.jsonl"),
    botToken: "test-not-a-secret",
    adminId: 1,
    chatIds: [-100],
    anthropicKey: "test",
    anthropicUrl: "https://example.invalid",
    model: "test-model",
    systemPrompt: "test-policy",
    chatPrompts: {},
    telegramApiRoot: "https://example.invalid",
    pollTimeout: 1,
    concurrency: 1,
    followupTtlMs: 1_000,
    classificationTimeoutMs: 5_000,
    maxClassificationAttempts: 2,
    maxActionAttempts: 2,
    retryBaseMs: 10,
    recoveryIntervalMs: 100,
    ...overrides,
  };
}

function open(settings: Settings, clock: { now: number }): Store {
  const store = new Store(settings, () => clock.now);
  stores.add(store);
  return store;
}

function restart(store: Store, settings: Settings, clock: { now: number }): Store {
  store.close();
  stores.delete(store);
  return open(settings, clock);
}

function message(messageId = 10, extras: Partial<Observation> = {}): Observation {
  return {
    source: "observer",
    sourceKey: `message:${messageId}`,
    kind: "message",
    chatId: -100,
    messageId,
    userId: 42,
    actorKind: "user",
    text: "hello",
    occurredAt: 10_000,
    ...extras,
  };
}

function verdict(
  job: ClassificationJob,
  result: Classification["verdict"] = "spam",
): Classification {
  return {
    verdict: result,
    reason: result === "spam" ? "Unsolicited commercial link" : "No spam evidence",
    evidenceMessageIds: job.snapshot.messages.map((item) => item.messageId),
    model: "test-model",
    promptVersion: "test-policy",
  };
}

class EvidenceClassifier implements ClassifierPort {
  readonly snapshots: Snapshot[] = [];
  classify(snapshot: Snapshot): Effect.Effect<Classification> {
    return Effect.sync(() => {
      this.snapshots.push(snapshot);
      const spam = snapshot.messages.some(
        (item) =>
          item.text.includes("spam") || JSON.stringify(item.metadata).includes("scam.example"),
      );
      return {
        verdict: spam ? "spam" : "clean",
        reason: spam ? "Commercial promotion" : "Ordinary conversation",
        evidenceMessageIds: snapshot.messages.map((item) => item.messageId),
        model: "test-model",
        promptVersion: "test-policy",
      };
    });
  }
}

/** Stateful external-service substitute: tests assert final effects, not echoed calls. */
class TelegramMemory implements TelegramPort {
  readonly botId = 777;
  readonly botUsername = "moderator_test_bot";
  readonly restricted = new Set<string>();
  readonly bans: Array<[number, number]> = [];
  readonly unbans: Array<[number, number]> = [];
  readonly deleted: Array<[number, number]> = [];
  readonly notices: Array<[number, string]> = [];
  readonly edits: Array<[number, number, string]> = [];
  notifyError: AppError | undefined;
  banError: AppError | undefined;
  async ban(chatId: number, userId: number, signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    if (this.banError) throw this.banError;
    this.bans.push([chatId, userId]);
    this.restricted.add(`${chatId}:${userId}`);
  }
  async unban(chatId: number, userId: number, signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    this.unbans.push([chatId, userId]);
    this.restricted.delete(`${chatId}:${userId}`);
  }
  async membership(chatId: number, userId: number, signal: AbortSignal): Promise<Membership> {
    signal.throwIfAborted();
    const restricted = this.restricted.has(`${chatId}:${userId}`);
    return { restricted, isMember: !restricted };
  }
  async delete(chatId: number, messageId: number, signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    this.deleted.push([chatId, messageId]);
  }
  async edit(chatId: number, messageId: number, text: string, signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    this.edits.push([chatId, messageId, text]);
    const index = messageId - 9_001;
    if (this.notices[index]) this.notices[index] = [chatId, text];
  }
  async notify(chatId: number, text: string, signal: AbortSignal): Promise<number> {
    signal.throwIfAborted();
    if (this.notifyError) throw this.notifyError;
    this.notices.push([chatId, text]);
    return 9_000 + this.notices.length;
  }
  async isAdmin(_chatId: number, userId: number, signal: AbortSignal): Promise<boolean> {
    signal.throwIfAborted();
    return userId === 1;
  }
}

async function drain(engine: Engine, turns = 6): Promise<void> {
  for (let index = 0; index < turns; index++) await Effect.runPromise(engine.tick());
}

describe("durable moderation state machine", () => {
  test("unknown observer-only user enrolls without a join; clean is never permanent approval", async () => {
    const settings = configuration();
    const clock = { now: 100 };
    const store = open(settings, clock);
    const classifier = new EvidenceClassifier();
    const telegram = new TelegramMemory();
    const engine = new Engine(store, classifier, telegram, settings);
    store.ingest(message());
    await drain(engine);
    expect(store.status().subjects).toBe(1);
    expect(store.status().approved).toBe(0);
    expect(store.decisionsFor(42).map((decision) => decision.verdict)).toEqual(["clean"]);
    store.ingest(message(11, { text: "spam campaign" }));
    await drain(engine);
    expect(classifier.snapshots).toHaveLength(2);
    expect(telegram.restricted.has("-100:42")).toBe(true);
  });

  test("milk replay from observer and bot is one verdict; bot actors and inline links are evidence", async () => {
    const settings = configuration();
    const store = open(settings, { now: 100 });
    const classifier = new EvidenceClassifier();
    const telegram = new TelegramMemory();
    const engine = new Engine(store, classifier, telegram, settings);
    const milk = message(12, {
      text: "Молоко сегодня бесплатно",
      metadata: { isBot: true, inlineButtons: [{ text: "Получить", url: "https://scam.example" }] },
    });
    store.ingest(milk);
    store.ingest({ ...milk, source: "bot", sourceKey: "update:44" });
    store.ingest({ ...milk, source: "replay", sourceKey: "milk-incident" });
    expect(store.ingest(milk)).toBe(false);
    await drain(engine);
    expect(classifier.snapshots).toHaveLength(1);
    expect(classifier.snapshots[0]!.messages[0]!.text).toBe("Молоко сегодня бесплатно");
    expect(store.decisionsFor(42)).toHaveLength(1);
    expect(telegram.bans).toEqual([[-100, 42]]);
    expect(telegram.deleted).toContainEqual([-100, 12]);
  });

  test("approval is explicit, archives later messages, and unapproval only checks new evidence", () => {
    const store = open(configuration(), { now: 100 });
    store.approve(-100, 42, 1);
    store.ingest(message(1, { text: "spam while approved" }));
    expect(store.claimClassification()).toBeUndefined();
    expect(store.status().messages).toBe(1);
    store.unapprove(-100, 42, 1);
    expect(store.claimClassification()).toBeUndefined();
    store.ingest(message(2, { text: "new spam" }));
    expect(store.claimClassification()!.snapshot.messages.map((item) => item.messageId)).toEqual([
      2,
    ]);
  });

  test("commands and self messages are archived; channels never become bannable users", () => {
    const store = open(configuration(), { now: 100 });
    store.bindBot(777);
    store.ingest(message(1, { userId: 777 }));
    store.ingest(message(2, { metadata: { isCommand: true }, text: "/approve" }));
    store.ingest(
      message(3, { actorKind: "channel", userId: undefined, text: "channel promotion" }),
    );
    expect(store.status().messages).toBe(3);
    expect(store.claimClassification()).toBeUndefined();
  });

  test("one in-flight job per subject; new messages do not invalidate a positive snapshot", () => {
    const store = open(configuration(), { now: 100 });
    store.ingest(message(1));
    const first = store.claimClassification()!;
    store.ingest(message(2));
    expect(store.claimClassification()).toBeUndefined();
    expect(store.completeClassification(first, verdict(first))).toBe(true);
    expect(store.claimClassification()).toBeUndefined();
    expect(store.status().actions_pending).toBe(4); // one ban, two message deletes, one notice
    store.ingest(message(3));
    expect(store.status().actions_pending).toBe(5); // cleanup, not another ban
  });

  test("a clean in-flight result schedules only newer revisions and never cancels a pending ban", () => {
    const store = open(configuration(), { now: 100 });
    store.ingest(message(1));
    const first = store.claimClassification()!;
    store.ingest(message(2));
    store.completeClassification(first, verdict(first, "clean"));
    const second = store.claimClassification()!;
    expect(second.snapshot.messages.map((item) => item.messageId)).toEqual([2]);
    store.completeClassification(second, verdict(second));
    expect(store.completeClassification(first, verdict(first, "clean"))).toBe(false);
    expect(store.claimAction()!.kind).toBe("ban");
  });

  test("edits are new immutable evidence, including same-second text changes", () => {
    const store = open(configuration(), { now: 100 });
    store.ingest(message(1));
    const first = store.claimClassification()!;
    store.ingest(
      message(1, { sourceKey: "edit:1", kind: "edit", text: "spam replacement", editedAt: 10_000 }),
    );
    expect(first.snapshot.messages[0]!.text).toBe("hello");
    store.completeClassification(first, verdict(first, "clean"));
    const edit = store.claimClassification()!;
    expect(edit.snapshot.messages[0]).toMatchObject({
      messageId: 1,
      revision: 2,
      text: "spam replacement",
    });
    store.completeClassification(edit, verdict(edit));
    expect(store.decisionForMessage(-100, 1)!.verdict).toBe("spam");
  });

  test("observer image evidence is enriched without losing a blob or duplicating an already usable revision", () => {
    const store = open(configuration(), { now: 100 });
    store.ingest(message(1, { media: [{ kind: "photo", blobHash: "abc" }] }));
    store.ingest(
      message(1, {
        source: "bot",
        sourceKey: "bot-photo",
        media: [{ kind: "photo", fileId: "telegram-file" }],
      }),
    );
    const first = store.claimClassification()!;
    expect(first.snapshot.messages[0]!.media[0]).toMatchObject({
      fileId: "telegram-file",
      blobHash: "abc",
    });
    store.completeClassification(first, verdict(first, "clean"));
    store.ingest(
      message(1, { sourceKey: "observer-receipt", media: [{ kind: "photo", blobHash: "abc" }] }),
    );
    expect(store.claimClassification()).toBeUndefined();
  });

  test("late usable image data rechecks previously uncertain image-only evidence", () => {
    const store = open(configuration(), { now: 100 });
    store.ingest(message(1, { text: "", media: [{ kind: "photo" }] }));
    const unavailable = store.claimClassification()!;
    store.completeClassification(unavailable, verdict(unavailable, "uncertain"));
    store.ingest(
      message(1, {
        source: "bot",
        sourceKey: "photo-file",
        text: "",
        media: [{ kind: "photo", fileId: "available" }],
      }),
    );
    const available = store.claimClassification()!;
    expect(available.snapshot.messages[0]!.revision).toBe(2);
    expect(available.snapshot.messages[0]!.media[0]!.fileId).toBe("available");
  });

  test("override invalidates a newer in-flight classification and suppresses that snapshot", () => {
    const store = open(configuration(), { now: 100 });
    store.ingest(message(1));
    const first = store.claimClassification()!;
    store.completeClassification(first, verdict(first, "clean"));
    const decision = store.decisionsFor(42)[0]!;
    store.ingest(message(2));
    const stale = store.claimClassification()!;
    store.override(decision.id, 1);
    expect(store.completeClassification(stale, verdict(stale))).toBe(false);
    expect(store.claimClassification()).toBeUndefined();
    store.ingest(message(3));
    expect(store.claimClassification()!.snapshot.messages.map((item) => item.messageId)).toEqual([
      3,
    ]);
    expect(store.getDecision(decision.id)!.overriddenAt).toBe(100);
  });

  test("override cancels an unsent ban without a Telegram mutation", async () => {
    const settings = configuration();
    const store = open(settings, { now: 100 });
    const telegram = new TelegramMemory();
    const engine = new Engine(store, new EvidenceClassifier(), telegram, settings);
    store.ingest(message());
    const job = store.claimClassification()!;
    store.completeClassification(job, verdict(job));
    const decision = store.decisionsFor(42)[0]!;
    store.override(decision.id, 1);
    await drain(engine);
    expect(telegram.bans).toHaveLength(0);
    expect(telegram.unbans).toHaveLength(0);
    expect(store.status().actions_cancelled).toBe(3);
  });

  test("undo during a ban preserves its late receipt then compensates exactly the owned restriction", async () => {
    const settings = configuration();
    const store = open(settings, { now: 100 });
    const telegram = new TelegramMemory();
    const engine = new Engine(store, new EvidenceClassifier(), telegram, settings);
    store.ingest(message());
    const job = store.claimClassification()!;
    store.completeClassification(job, verdict(job));
    const ban = store.claimAction()!;
    expect(store.prepareAction(ban, { restricted: false, isMember: true })).toBe(true);
    store.override(ban.decisionId, 1);
    await telegram.ban(-100, 42, new AbortController().signal);
    store.completeAction(ban);
    await drain(engine);
    expect(telegram.bans).toEqual([[-100, 42]]);
    expect(telegram.unbans).toEqual([[-100, 42]]);
    expect(telegram.restricted.has("-100:42")).toBe(false);
  });

  test("foreign restriction observed after our receipt prevents undo", async () => {
    const settings = configuration();
    const store = open(settings, { now: 100 });
    const telegram = new TelegramMemory();
    const engine = new Engine(store, new EvidenceClassifier(), telegram, settings);
    store.ingest(message(1, { text: "spam" }));
    await Effect.runPromise(engine.tick());
    store.ingest({
      source: "bot",
      sourceKey: "foreign-ban",
      kind: "membership",
      chatId: -100,
      userId: 42,
      occurredAt: 101,
      membership: "banned",
      changedBy: 99,
    });
    store.override(store.decisionsFor(42)[0]!.id, 1);
    await drain(engine);
    expect(telegram.unbans).toHaveLength(0);
    expect(telegram.restricted.has("-100:42")).toBe(true);
    expect(store.status().actions_needs_attention).toBe(1);
  });

  test("foreign restriction racing membership preflight cannot be overwritten and claimed", () => {
    const store = open(configuration(), { now: 100 });
    store.bindBot(777);
    store.ingest(message());
    const job = store.claimClassification()!;
    store.completeClassification(job, verdict(job));
    const ban = store.claimAction()!;
    store.ingest({
      source: "bot",
      sourceKey: "racing-ban",
      kind: "membership",
      chatId: -100,
      userId: 42,
      occurredAt: 100,
      membership: "banned",
      changedBy: 99,
    });
    expect(store.prepareAction(ban, { restricted: false, isMember: true })).toBe(false);
    expect(store.action(ban.id)).toMatchObject({ state: "needs_attention", owned: 0 });
  });

  test("restart at the last classification attempt retries slowly rather than stranding or trusting", () => {
    const settings = configuration({ maxClassificationAttempts: 1 });
    const clock = { now: 100 };
    let store = open(settings, clock);
    store.ingest(message());
    const interrupted = store.claimClassification()!;
    expect(interrupted.attempt).toBe(1);
    store = restart(store, settings, clock);
    expect(store.claimClassification()).toBeUndefined();
    expect(store.status().approved).toBe(0);
    clock.now = 200;
    const recovery = store.claimClassification()!;
    expect(recovery.attempt).toBe(2);
    expect(recovery.snapshot.id).toBe(interrupted.snapshot.id);
    expect(store.completeClassification(interrupted, verdict(interrupted))).toBe(false);
    store.completeClassification(recovery, verdict(recovery, "clean"));
    expect(store.decisionsFor(42)).toHaveLength(1);
  });

  test("final-attempt interrupted ban reconciles; an existing ban never becomes owned by inference", () => {
    const settings = configuration({ maxActionAttempts: 1 });
    const clock = { now: 100 };
    let store = open(settings, clock);
    store.ingest(message());
    const job = store.claimClassification()!;
    store.completeClassification(job, verdict(job));
    const interrupted = store.claimAction()!;
    store.prepareAction(interrupted, { restricted: false, isMember: true });
    store = restart(store, settings, clock);
    const recovery = store.claimAction()!;
    expect(recovery.phase).toBe("reconcile");
    expect(recovery.attempt).toBe(2);
    expect(store.prepareAction(recovery, { restricted: true, isMember: false })).toBe(false);
    store.override(recovery.decisionId, 1);
    expect(store.action(recovery.id)).toMatchObject({ state: "needs_attention", owned: 0 });
    expect(store.claimAction()).toBeUndefined();
  });

  test("absent interrupted ban retries only after reconciliation and recovery delay", () => {
    const settings = configuration({ maxActionAttempts: 1 });
    const clock = { now: 100 };
    let store = open(settings, clock);
    store.ingest(message());
    const job = store.claimClassification()!;
    store.completeClassification(job, verdict(job));
    const interrupted = store.claimAction()!;
    store.prepareAction(interrupted, { restricted: false, isMember: true });
    store = restart(store, settings, clock);
    const reconcile = store.claimAction()!;
    expect(store.prepareAction(reconcile, { restricted: false, isMember: true })).toBe(false);
    expect(store.action(reconcile.id)).toMatchObject({ state: "retry", retryAt: 200 });
    clock.now = 200;
    const retry = store.claimAction()!;
    expect(retry.kind).toBe("ban");
    expect(retry.phase).toBe("preflight");
    expect(store.prepareAction(retry, { restricted: false, isMember: true })).toBe(true);
  });

  test("classifier failure remains durable and does not grant trust", async () => {
    const settings = configuration();
    const clock = { now: 100 };
    const store = open(settings, clock);
    const classifier: ClassifierPort = {
      classify() {
        return Effect.fail(new AppError({ category: "transient", message: "model unavailable" }));
      },
    };
    const engine = new Engine(store, classifier, new TelegramMemory(), settings);
    store.ingest(message());
    await expect(Effect.runPromise(engine.tick())).rejects.toThrow("model unavailable");
    expect(store.status().jobs_retry).toBe(1);
    expect(store.status().approved).toBe(0);
    expect(store.decisionsFor(42)).toEqual([]);
    expect(store.claimClassification()).toBeUndefined();
    clock.now = 110;
    expect(store.claimClassification()!.attempt).toBe(2);
  });

  test("Effect interruption aborts the port and persists a retriable job", async () => {
    const settings = configuration();
    const clock = { now: 100 };
    const store = open(settings, clock);
    let start: () => void = () => {
      throw new Error("start latch not initialized");
    };
    const started = new Promise<void>((resolve) => {
      start = resolve;
    });
    let aborted = false;
    const classifier: ClassifierPort = {
      classify() {
        return Effect.sync(start).pipe(
          Effect.andThen(Effect.never),
          Effect.onInterrupt(() =>
            Effect.sync(() => {
              aborted = true;
            }),
          ),
        );
      },
    };
    const engine = new Engine(store, classifier, new TelegramMemory(), settings);
    store.ingest(message());
    const fiber = Effect.runFork(engine.tick());
    await started;
    await Effect.runPromise(Fiber.interrupt(fiber));
    expect(aborted).toBe(true);
    expect(store.status().jobs_retry).toBe(1);
    clock.now = 110;
    expect(store.claimClassification()!.attempt).toBe(2);
  });

  test("pending actions execute when monitoring is disabled and notification failure never rolls back a ban", async () => {
    const settings = configuration();
    const clock = { now: 100 };
    const store = open(settings, clock);
    const telegram = new TelegramMemory();
    const engine = new Engine(store, new EvidenceClassifier(), telegram, settings);
    store.ingest(message());
    const job = store.claimClassification()!;
    store.completeClassification(job, verdict(job));
    store.toggleChat(-100, false);
    telegram.notifyError = new AppError({ category: "permanent", message: "cannot notify" });
    await Effect.runPromise(engine.tick());
    await Effect.runPromise(engine.tick());
    await expect(Effect.runPromise(engine.tick())).rejects.toThrow("cannot notify");
    expect(telegram.restricted.has("-100:42")).toBe(true);
    expect(store.status().actions_confirmed).toBe(2);
    expect(store.status().actions_blocked).toBe(1);
  });

  test("unclaimed and interrupted shadow evidence retains shadow origin after live restart", async () => {
    const shadow = configuration({ mode: "shadow" });
    const clock = { now: 100 };
    let store = open(shadow, clock);
    store.ingest(message(1, { text: "spam interrupted shadow" }));
    store.ingest(message(2, { userId: 43, text: "spam unclaimed shadow" }));
    store.claimClassification();
    const live = { ...shadow, mode: "live" as const };
    store = restart(store, live, clock);
    const telegram = new TelegramMemory();
    const engine = new Engine(store, new EvidenceClassifier(), telegram, live);
    await drain(engine);
    expect(telegram.bans).toHaveLength(0);
    expect(store.status().actions_shadow).toBe(6);
    store.ingest(message(3, { text: "spam new live evidence" }));
    await drain(engine);
    expect(telegram.bans).toEqual([[-100, 42]]);
  });

  test("shadow actions are never executed after reopening in live mode; future evidence can act", async () => {
    const shadow = configuration({ mode: "shadow" });
    const clock = { now: 100 };
    let store = open(shadow, clock);
    store.ingest(message(1, { text: "spam shadow" }));
    const shadowTelegram = new TelegramMemory();
    await drain(new Engine(store, new EvidenceClassifier(), shadowTelegram, shadow));
    expect(store.status().actions_shadow).toBe(3);
    expect(shadowTelegram.bans).toHaveLength(0);
    const live = { ...shadow, mode: "live" as const };
    store = restart(store, live, clock);
    const telegram = new TelegramMemory();
    const engine = new Engine(store, new EvidenceClassifier(), telegram, live);
    await drain(engine);
    expect(telegram.bans).toHaveLength(0);
    expect(store.status().actions_shadow).toBe(3);
    store.ingest(message(2, { text: "spam after activation" }));
    await drain(engine);
    expect(telegram.bans).toEqual([[-100, 42]]);
  });

  test("notification follow-up deletion survives restart and an undo", async () => {
    const settings = configuration();
    const clock = { now: 100 };
    let store = open(settings, clock);
    const telegram = new TelegramMemory();
    store.ingest(message(1, { text: "spam" }));
    await drain(new Engine(store, new EvidenceClassifier(), telegram, settings));
    expect(telegram.notices[0]![1]).toContain("start=explain_");
    expect(telegram.notices[0]![1]).not.toContain("Commercial promotion");
    store.override(store.decisionsFor(42)[0]!.id, 1);
    store = restart(store, settings, clock);
    const engine = new Engine(store, new EvidenceClassifier(), telegram, settings);
    await drain(engine);
    expect(telegram.deleted).not.toContainEqual([-100, 9_001]);
    expect(telegram.edits).toEqual([[-100, 9_001, "Решение отменено"]]);
    expect(telegram.notices[0]![1]).toBe("Решение отменено");
    expect(store.decisionsFor(42)[0]!.verdict).toBe("spam");
    clock.now = 1_100;
    await drain(engine);
    expect(telegram.deleted).toContainEqual([-100, 9_001]);
  });

  test("failed batch rolls back earlier observations, commands, and offset together", () => {
    const store = open(configuration(), { now: 100 });
    const cyclic: { self?: unknown } = {};
    cyclic.self = cyclic;
    expect(() =>
      store.acceptBatch([message(1), message(2, { raw: cyclic })], 20, [
        { id: "cmd", chatId: -100, userId: 1, text: "/toggle_bot" },
      ]),
    ).toThrow();
    expect(store.getOffset()).toBe(0);
    expect(store.status().messages).toBe(0);
    expect(store.claimCommand()).toBeUndefined();
  });

  test("only an observed own-bot exact notification can recover an unknown send receipt", async () => {
    const settings = configuration();
    const clock = { now: 20_000 };
    const store = open(settings, clock);
    const telegram = new TelegramMemory();
    const engine = new Engine(store, new EvidenceClassifier(), telegram, settings);
    store.ingest(message(1, { text: "spam" }));
    await Effect.runPromise(engine.tick());
    await Effect.runPromise(engine.tick());
    const notify = store.claimAction()!;
    expect(notify.kind).toBe("notify");
    store.prepareAction(notify);
    store.failAction(
      notify,
      new AppError({ category: "unknown", message: "response lost after send" }),
    );
    const text = `Moderation decision recorded. Details and appeal: https://t.me/${telegram.botUsername}?start=explain_${notify.decisionId}`;
    store.ingest(
      message(91, { sourceKey: "copied-link", userId: 99, text, occurredAt: clock.now }),
    );
    expect(store.action(notify.id)).toMatchObject({ state: "needs_attention" });
    store.override(notify.decisionId, 1);
    store.ingest(
      message(92, {
        sourceKey: "actual-notice",
        userId: telegram.botId,
        metadata: { isSelf: true },
        text,
        occurredAt: clock.now,
      }),
    );
    expect(store.action(notify.id)).toMatchObject({ state: "confirmed", payload: { receipt: 92 } });
    await drain(engine);
    expect(telegram.edits).toContainEqual([-100, 92, "Решение отменено"]);
    expect(telegram.notices).toHaveLength(0);
    clock.now += settings.followupTtlMs;
    await drain(engine);
    expect(telegram.deleted).toContainEqual([-100, 92]);
  });

  test("acceptBatch offset, observation and command are atomic; mutation does not toggle twice after retry", () => {
    const settings = configuration();
    const clock = { now: 100 };
    let store = open(settings, clock);
    const command = { id: "update:9", chatId: -100, userId: 1, text: "/toggle_bot" };
    store.acceptBatch([message()], 10, [command]);
    store.acceptBatch([message()], 8, [command]);
    expect(store.getOffset()).toBe(10);
    expect(store.status().messages).toBe(1);
    expect(store.claimCommand()!.id).toBe("update:9");
    expect(store.applyCommand(command.id, () => store.toggleChat(-100))).toBe(false);
    store = restart(store, settings, clock);
    expect(store.claimCommand()!.id).toBe("update:9");
    expect(store.applyCommand(command.id, () => store.toggleChat(-100))).toBeUndefined();
    expect(store.chatEnabled(-100)).toBe(false);
    store.finishCommand(command.id);
    expect(store.claimCommand()).toBeUndefined();
  });

  test("bot identity is persisted, and mismatch cannot consume another bot's offset", () => {
    const settings = configuration();
    const clock = { now: 100 };
    let store = open(settings, clock);
    store.bindBot(777);
    store.acceptBatch([], 44);
    store = restart(store, settings, clock);
    expect(() => store.bindBot(888)).toThrow("another Telegram bot");
    expect(store.getOffset()).toBe(44);
    store.bindBot(777);
  });

  test("migration keeps user identity, routes bans to new chat, and never rewrites old message deletion IDs", async () => {
    const settings = configuration();
    const store = open(settings, { now: 100 });
    const telegram = new TelegramMemory();
    const classifier = new EvidenceClassifier();
    const engine = new Engine(store, classifier, telegram, settings);
    store.ingest(message(1, { text: "spam before migration" }));
    store.ingest({
      source: "bot",
      sourceKey: "migration",
      kind: "migration",
      chatId: -100,
      migratedTo: -200,
      occurredAt: 11_000,
    });
    store.ingest(
      message(1, { sourceKey: "new-chat-message", chatId: -200, text: "spam after migration" }),
    );
    expect(store.tracksChat(-200)).toBe(true);
    expect(store.status(-200).subjects).toBe(1);
    expect(store.status(-200).messages).toBe(2);
    await drain(engine);
    expect(telegram.bans).toEqual([[-200, 42]]);
    expect(telegram.deleted).toContainEqual([-100, 1]);
    expect(telegram.deleted).toContainEqual([-200, 1]);
    expect(store.decisionsFor(42)[0]!.chatId).toBe(-200);
    expect(classifier.snapshots[0]!.messages).toHaveLength(1);
    expect(classifier.snapshots[0]!.messages[0]!.text).toBe("spam after migration");
  });

  test("legacy import is read-only and idempotent; only legacy approval confers trust", () => {
    const settings = configuration();
    const sourcePath = join(directories[directories.length - 1]!, "legacy.sqlite");
    const source = new Database(sourcePath);
    source.exec(`CREATE TABLE approved_users(id INTEGER PRIMARY KEY,user_id INTEGER,chat_id INTEGER);
      INSERT INTO approved_users VALUES(1,42,-100);
      CREATE TABLE new_users(id INTEGER PRIMARY KEY,user_id INTEGER,chat_id INTEGER);
      INSERT INTO new_users VALUES(1,43,-100);
      CREATE TABLE message_queue(id INTEGER PRIMARY KEY,user_id INTEGER,chat_id INTEGER,message_id INTEGER,message_text TEXT,status TEXT,created_at TEXT);
      INSERT INTO message_queue VALUES(1,43,-100,4,'legacy pending spam','processing','2025-01-01 00:00:00');
      CREATE TABLE spam_check_results(id TEXT PRIMARY KEY,user_id INTEGER,chat_id INTEGER,is_spam INTEGER,reason TEXT,checked_at TEXT);
      INSERT INTO spam_check_results VALUES('old-clean',43,-100,0,'Legacy clean','2025-01-01 00:00:00');`);
    source.close();
    const before = readFileSync(sourcePath);
    const store = open(settings, { now: 100 });
    const counts = store.importLegacy(sourcePath);
    expect(counts.approved_users).toBe(1);
    expect(counts.message_queue).toBe(1);
    expect(store.importLegacy(sourcePath).approved_users).toBe(0);
    expect(readFileSync(sourcePath).equals(before)).toBe(true);
    expect(store.status().approved).toBe(1);
    expect(store.decisionsFor(43)[0]!.promptVersion).toBe("legacy");
    expect(store.claimClassification()!.snapshot.userId).toBe(43);
  });

  test("journal cursor exports every durable event once in ordinary operation and resumes on reopen", () => {
    const settings = configuration();
    const clock = { now: 100 };
    let store = open(settings, clock);
    store.ingest(message());
    const first = store.exportJournal();
    expect(first).toBeGreaterThan(0);
    expect(store.exportJournal()).toBe(0);
    store = restart(store, settings, clock);
    expect(store.exportJournal()).toBe(0);
    store.approve(-100, 42, 1);
    expect(store.exportJournal()).toBeGreaterThan(0);
    const lines = readFileSync(settings.journalPath, "utf8").trim().split("\n");
    expect(lines.some((line) => line.includes('"kind":"approved"'))).toBe(true);
    expect(lines.filter((line) => line.includes('"kind":"observation"'))).toHaveLength(1);
  });
});
