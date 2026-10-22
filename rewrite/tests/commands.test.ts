import { describe, expect, test } from "bun:test";
import { GrammyError, type Api } from "grammy";
import { handleCommand } from "../src/commands.ts";
import {
  AppError,
  type ActionDetail,
  type Command,
  type Decision,
  type Settings,
  type TelegramPort,
} from "../src/domain.ts";
import type { Store } from "../src/store.ts";

const decisionId = "12345678-1234-1234-1234-123456789abc";
const actionId = "12345678-1234-1234-1234-123456789def";
const settings: Settings = {
  mode: "shadow",
  dbPath: ":memory:",
  botToken: "private-bot-token",
  adminId: 99,
  chatIds: [-100, -200],
  anthropicKey: "private-model-token",
  anthropicUrl: "https://api.anthropic.com",
  model: "test",
  systemPrompt: "test",
  chatPrompts: {},
  telegramApiRoot: "https://api.telegram.org",
  pollTimeout: 25,
  concurrency: 1,
  followupTtlMs: 60_000,
  classificationTimeoutMs: 30_000,
  maxClassificationAttempts: 3,
  maxActionAttempts: 5,
  retryBaseMs: 5000,
  recoveryIntervalMs: 300_000,
  journalPath: "/unused",
};
const decision: Decision = {
  id: decisionId,
  chatId: -100,
  userId: 7,
  snapshotId: "snapshot",
  createdAt: 1_700_000_000_000,
  overriddenAt: null,
  status: "shadow",
  verdict: "spam",
  reason: "Private evidence private-bot-token",
  evidenceMessageIds: [10],
  model: "test",
  promptVersion: "test",
};

const action: ActionDetail = {
  id: actionId,
  kind: "ban",
  decisionId,
  chatId: -100,
  userId: 7,
  generation: 1,
  messageId: null,
  messageChatId: null,
  state: "retry",
  attempt: 1,
  token: "private-action-token",
  payload: { reason: "Private action evidence" },
  mode: "shadow",
  retryAt: 0,
  error: "Private delivery error",
  undoRequested: 0,
  owned: 0,
  ownershipEpoch: null,
  preflightEpoch: null,
  sentAt: null,
  confirmedAt: null,
  createdAt: decision.createdAt,
  attempts: [
    {
      attempt: 1,
      startedAt: decision.createdAt,
      finishedAt: null,
      outcome: null,
      error: "Private attempt error",
    },
  ],
};

function fixture(isAdmin = false, migrated = false) {
  const sent: { chatId: number; text: string; options?: Record<string, unknown> }[] = [];
  const answers: string[] = [];
  const authorization: { chatId: number; userId: number }[] = [];
  const overrides: { decisionId: string; actorId: number }[] = [];
  const approvals: { chatId: number; userId: number; actorId: number }[] = [];
  const unapprovals: { chatId: number; userId: number; actorId: number }[] = [];
  const toggles: { chatId: number; enabled: boolean }[] = [];
  const statusRequests: (number | undefined)[] = [];
  const messageLookups: { chatId: number; messageId: number }[] = [];
  const historyRequests: number[] = [];
  let enabled = true;
  const applied = new Set<string>();
  const store = {
    tracksChat: (chatId: number) =>
      settings.chatIds.includes(chatId) || (migrated && chatId === -300),
    resolveChatId: (chatId: number) => (migrated && chatId === -100 ? -300 : chatId),
    getDecision: (id: string) => (id === decisionId ? decision : undefined),
    decisionForMessage: (chatId: number, messageId: number) => {
      messageLookups.push({ chatId, messageId });
      return messageId === 10 ? decision : undefined;
    },
    decisionsFor: (userId: number) => {
      historyRequests.push(userId);
      return userId === decision.userId ? [decision] : [];
    },
    action: (id: string) => (id === actionId ? action : undefined),
    status: (chatId?: number) => {
      statusRequests.push(chatId);
      return { pending: 3 };
    },
    toggleChat: (chatId: number, requested?: boolean) => {
      enabled = requested ?? !enabled;
      toggles.push({ chatId, enabled });
      return enabled;
    },
    chatEnabled: () => enabled,
    applyCommand: <T>(id: string, mutation: () => T): T | undefined => {
      if (applied.has(id)) return undefined;
      const result = mutation();
      applied.add(id);
      return result;
    },
    override: (id: string, actorId: number) => {
      overrides.push({ decisionId: id, actorId });
    },
    approve: (chatId: number, userId: number, actorId: number) => {
      approvals.push({ chatId, userId, actorId });
    },
    unapprove: (chatId: number, userId: number, actorId: number) => {
      unapprovals.push({ chatId, userId, actorId });
    },
  } as unknown as Store;
  const telegram: TelegramPort = {
    botId: 500,
    botUsername: "test_misaka_bot",
    isAdmin: async (chatId, userId) => {
      authorization.push({ chatId, userId });
      return isAdmin;
    },
    ban: async () => {
      throw new Error("Commands must never ban directly");
    },
    unban: async () => {
      throw new Error("Commands must never unban directly");
    },
    membership: async () => {
      throw new Error("Commands must not reconcile moderation");
    },
    delete: async () => {
      throw new Error("Commands must not delete directly");
    },
    edit: async () => {
      throw new Error("Commands must not edit notifications directly");
    },
    notify: async () => {
      throw new Error("Commands use plain-text API replies");
    },
  };
  const api = {
    sendMessage: async (chatId: number, text: string, options?: Record<string, unknown>) => {
      sent.push({ chatId, text, options });
      return { message_id: sent.length };
    },
    answerCallbackQuery: async (_id: string, options: { text: string }) => {
      answers.push(options.text);
      return true;
    },
  } as unknown as Api;
  return {
    sent,
    answers,
    authorization,
    overrides,
    approvals,
    unapprovals,
    toggles,
    statusRequests,
    messageLookups,
    historyRequests,
    telegram,
    api,
    run: (command: Partial<Command>, signal = new AbortController().signal) =>
      handleCommand(
        {
          id: "update-1",
          chatId: 7,
          userId: 7,
          text: `/explain ${decisionId}`,
          ...command,
        },
        store,
        telegram,
        api,
        settings,
        signal,
      ),
  };
}

describe("command authorization and privacy", () => {
  test("unrelated private users receive only public details and no undo control", async () => {
    const f = fixture();
    await f.run({ userId: 8, chatId: 8 });
    expect(f.sent[0]!.text).toContain(decisionId);
    expect(f.sent[0]!.text).toContain(decision.status);
    expect(f.sent[0]!.text).not.toContain("Private evidence");
    expect(f.sent[0]!.text).not.toContain("private-bot-token");
    expect(f.sent[0]!.options?.reply_markup).toBeUndefined();
    expect(f.sent[0]!.options?.parse_mode).toBeUndefined();
  });

  test("affected users see redacted full reasons privately, but cannot override", async () => {
    const f = fixture();
    await f.run({});
    expect(f.sent[0]!.text).toContain("Private evidence");
    expect(f.sent[0]!.text).not.toContain(settings.botToken);
    expect(f.sent[0]!.options?.reply_markup).toBeUndefined();
    await f.run({ id: "undo", callbackId: "callback", text: `undo:${decisionId}` });
    expect(f.overrides).toHaveLength(0);
  });

  test("group explanations remain sanitized even for global administrators", async () => {
    const f = fixture();
    await f.run({ chatId: -100, userId: 99 });
    expect(f.sent[0]!.text).not.toContain("Private evidence");
    expect(f.sent[0]!.options?.reply_markup).toBeUndefined();
  });

  test("verified decision-chat admins receive a private undo button", async () => {
    const f = fixture(true);
    await f.run({ chatId: 8, userId: 8, text: `/start explain_${decisionId}` });
    expect(f.authorization).toEqual([{ chatId: -100, userId: 8 }]);
    expect(f.sent[0]!.text).toContain("Private evidence");
    expect(f.sent[0]!.text).not.toContain(settings.botToken);
    expect(f.sent[0]!.options?.reply_markup).toEqual({
      inline_keyboard: [[expect.objectContaining({ callback_data: `undo:${decisionId}` })]],
    });
  });

  test("private undo reauthorizes against the decision chat and deduplicates replay", async () => {
    const f = fixture(true);
    const command = { chatId: 8, userId: 8, callbackId: "callback", text: `undo:${decisionId}` };
    await f.run(command);
    await f.run(command);
    expect(f.authorization).toEqual([
      { chatId: -100, userId: 8 },
      { chatId: -100, userId: 8 },
    ]);
    expect(f.overrides).toEqual([{ decisionId, actorId: 8 }]);
    expect(f.answers).toHaveLength(2);
  });

  test("forwarded undo controls cannot operate from another group", async () => {
    const f = fixture(true);
    await f.run({ chatId: -200, userId: 99, callbackId: "callback", text: `undo:${decisionId}` });
    expect(f.overrides).toHaveLength(0);
    expect(f.authorization).toHaveLength(0);
    expect(f.answers).toHaveLength(1);
  });

  test("global administrators bypass membership checks but not callback scope", async () => {
    const f = fixture();
    await f.run({ chatId: 99, userId: 99, callbackId: "callback", text: `undo:${decisionId}` });
    expect(f.authorization).toHaveLength(0);
    expect(f.overrides).toEqual([{ decisionId, actorId: 99 }]);
  });

  test("migration aliases authorize historical decisions against the current Telegram group", async () => {
    const f = fixture(true, true);
    await f.run({ chatId: -300, userId: 8, callbackId: "callback", text: `undo:${decisionId}` });
    expect(f.authorization).toEqual([{ chatId: -300, userId: 8 }]);
    expect(f.overrides).toEqual([{ decisionId, actorId: 8 }]);
    await f.run({ id: "approve", chatId: -300, userId: 8, text: "/approve 7" });
    expect(f.approvals).toEqual([{ chatId: -300, userId: 7, actorId: 8 }]);
  });

  test("replayed old-group commands reply to the migrated group without stale message IDs", async () => {
    const f = fixture(true, true);
    await f.run({ chatId: -100, userId: 8, messageId: 12 });
    expect(f.sent[0]!.chatId).toBe(-300);
    expect(f.sent[0]!.text).toContain(decisionId);
    expect(f.sent[0]!.options?.reply_parameters).toBeUndefined();
  });

  test("approval accepts reply identities only after actual chat-admin authorization", async () => {
    const denied = fixture();
    await denied.run({ chatId: -100, userId: 8, text: "/approve", replyToUserId: 7 });
    expect(denied.approvals).toHaveLength(0);
    const allowed = fixture(true);
    await allowed.run({ chatId: -100, userId: 8, text: "/approve", replyToUserId: 7 });
    expect(allowed.approvals).toEqual([{ chatId: -100, userId: 7, actorId: 8 }]);
  });

  test("approval never guesses a username or accepts a command for another bot", async () => {
    const f = fixture(true);
    await f.run({ chatId: -100, userId: 8, text: "/approve @somebody" });
    await f.run({ chatId: -100, userId: 8, text: "/approve@other_bot 7" });
    expect(f.approvals).toHaveLength(0);
    expect(f.sent).toHaveLength(1);
  });

  test("group explanations never follow references into another configured chat", async () => {
    const f = fixture(true);
    await f.run({ chatId: -200, userId: 8 });
    expect(f.sent).toHaveLength(1);
    expect(f.sent[0]!.text).not.toContain(decisionId);
    expect(f.sent[0]!.text).not.toContain("Private evidence");
    expect(f.authorization).toHaveLength(0);
  });

  test("message references resolve only inside a group, including reply references", async () => {
    const f = fixture();
    await f.run({ text: "/explain 10" });
    expect(f.messageLookups).toHaveLength(0);
    await f.run({ chatId: -100, text: "/explain 10" });
    await f.run({ chatId: -100, text: "/explain", replyToMessageId: 10 });
    expect(f.messageLookups).toEqual([
      { chatId: -100, messageId: 10 },
      { chatId: -100, messageId: 10 },
    ]);
    expect(f.sent[1]!.text).toContain(decisionId);
    expect(f.sent[2]!.text).toContain(decisionId);
    expect(f.sent.every((message) => !message.text.includes("Private evidence"))).toBe(true);
  });

  test("history queries only the private caller and never renders evidence", async () => {
    const f = fixture();
    await f.run({ text: "/start" });
    expect(f.historyRequests).toEqual([7]);
    expect(f.sent[0]!.text).toContain(decisionId);
    expect(f.sent[0]!.text).not.toContain("Private evidence");
    await f.run({ chatId: -100, text: "/start" });
    expect(f.historyRequests).toEqual([7]);
    expect(f.sent[1]!.text).not.toContain(decisionId);
  });

  test("action diagnostics require authority in the action chat and hide persisted secrets", async () => {
    const denied = fixture();
    await denied.run({ userId: 8, chatId: 8, text: `/action ${actionId}` });
    expect(denied.sent[0]!.text).not.toContain(actionId);
    expect(denied.authorization).toEqual([{ chatId: -100, userId: 8 }]);

    const allowed = fixture(true);
    await allowed.run({ userId: 8, chatId: 8, text: `/action ${actionId}` });
    const text = allowed.sent[0]!.text;
    expect(text).toContain(actionId);
    expect(text).toContain(decisionId);
    expect(text).not.toContain(action.token);
    expect(text).not.toContain(action.payload.reason!);
    expect(text).not.toContain(action.error!);
    expect(text).not.toContain(action.attempts[0]!.error!);

    await allowed.run({ userId: 8, chatId: -200, text: `/action ${actionId}` });
    expect(allowed.sent[1]!.text).not.toContain(actionId);
    expect(allowed.authorization).toHaveLength(1);
  });

  test("global administrators cannot mutate unconfigured groups", async () => {
    const f = fixture(true);
    await f.run({ userId: 99, chatId: -999, text: "/approve 7" });
    await f.run({ userId: 99, chatId: -999, text: "/unapprove 7" });
    await f.run({ userId: 99, chatId: -999, text: "/toggle_bot off" });
    expect(f.approvals).toHaveLength(0);
    expect(f.unapprovals).toHaveLength(0);
    expect(f.toggles).toHaveLength(0);
    expect(f.authorization).toHaveLength(0);
  });

  test("approval and removal deduplicate mutations while reauthorizing replay", async () => {
    const f = fixture(true);
    const approve = { id: "approve", chatId: -100, userId: 8, text: "/approve 7" };
    const unapprove = { ...approve, id: "unapprove", text: "/unapprove 7" };
    await f.run(approve);
    await f.run(approve);
    await f.run(unapprove);
    await f.run(unapprove);
    expect(f.authorization).toHaveLength(4);
    expect(f.approvals).toEqual([{ chatId: -100, userId: 7, actorId: 8 }]);
    expect(f.unapprovals).toEqual([{ chatId: -100, userId: 7, actorId: 8 }]);
  });

  test("toggle replay never flips state twice and explicit on or off remains supported", async () => {
    const f = fixture(true);
    const command = { id: "toggle", chatId: -100, userId: 8, text: "/toggle_bot" };
    await f.run(command);
    await f.run(command);
    await f.run({ ...command, id: "on", text: "/toggle_bot ON" });
    await f.run({ ...command, id: "off", text: "/toggle_bot off" });
    await f.run({ ...command, id: "invalid", text: "/toggle_bot invalid" });
    expect(f.toggles).toEqual([
      { chatId: -100, enabled: false },
      { chatId: -100, enabled: true },
      { chatId: -100, enabled: false },
    ]);
  });

  test("queue status is scoped to the authorized chat except for the private global admin", async () => {
    const f = fixture(true);
    await f.run({ userId: 8, chatId: -100, text: "/queue_status" });
    await f.run({ userId: 99, chatId: 99, text: "/queue_status" });
    await f.run({ userId: 8, chatId: 8, text: "/queue_status" });
    expect(f.statusRequests).toEqual([-100, undefined]);
    expect(f.authorization).toEqual([{ chatId: -100, userId: 8 }]);
  });

  test("authorization failures remain retryable and never leak SDK or application secrets", async () => {
    const f = fixture(true);
    f.telegram.isAdmin = async () => {
      throw new AppError({
        message: `Private failure ${settings.botToken}`,
        category: "transient",
        retryAfterMs: 5000,
      });
    };
    let failure: unknown;
    try {
      await f.run({ chatId: -100, userId: 8, text: "/approve 7" });
    } catch (error) {
      failure = error;
    }
    expect(failure).toBeInstanceOf(AppError);
    expect(failure).toMatchObject({ category: "transient", retryAfterMs: 5000 });
    expect(String(failure)).not.toContain(settings.botToken);
    expect(f.approvals).toHaveLength(0);
    expect(f.sent).toHaveLength(0);

    f.telegram.isAdmin = async () => true;
    await f.run({ chatId: -100, userId: 8, text: "/approve 7" });
    expect(f.approvals).toEqual([{ chatId: -100, userId: 7, actorId: 8 }]);
  });

  test("expired callback answers do not undo or repeat an already recorded mutation", async () => {
    const f = fixture(true);
    f.api.answerCallbackQuery = async () => {
      throw new GrammyError(
        "expired",
        {
          ok: false,
          error_code: 400,
          description: "expired",
        },
        "answerCallbackQuery",
        { callback_query_id: "callback" },
      );
    };
    const command = { chatId: 8, userId: 8, callbackId: "callback", text: `undo:${decisionId}` };
    await f.run(command);
    await f.run(command);
    expect(f.overrides).toEqual([{ decisionId, actorId: 8 }]);
  });

  test("interrupted authorization is transient even when its underlying error is permanent", async () => {
    const f = fixture();
    const controller = new AbortController();
    f.telegram.isAdmin = async () => {
      controller.abort();
      throw new AppError({ message: settings.botToken, category: "permanent" });
    };
    let failure: unknown;
    try {
      await f.run({ chatId: -100, userId: 8, text: "/approve 7" }, controller.signal);
    } catch (error) {
      failure = error;
    }
    expect(failure).toBeInstanceOf(AppError);
    expect(failure).toMatchObject({ category: "transient" });
    expect(String(failure)).not.toContain(settings.botToken);
    expect(f.approvals).toHaveLength(0);
  });
});
