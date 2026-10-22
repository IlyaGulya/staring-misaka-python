import { GrammyError, type Api } from "grammy";
import {
  AppError,
  type ActionDetail,
  type Command,
  type Decision,
  type Settings,
  type TelegramPort,
} from "./domain.ts";
import type { Store } from "./store.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HELP =
  "Commands: /explain <decision ID or message ID> (or reply to a message), /start for your decision history. Group administrators: /approve <user ID> or reply, /unapprove <user ID> or reply, /toggle_bot [on|off], /queue_status, /action <action ID>.";

type DecisionReference =
  | { kind: "decision"; id: string }
  | { kind: "message"; id: number }
  | { kind: "unavailable" }
  | { kind: "invalidArguments" };

type StartRequest =
  | { kind: "history" }
  | { kind: "explain"; reference: DecisionReference }
  | { kind: "invalidArguments" };

type ParsedCommand =
  | { kind: "undo"; decisionId: string | undefined }
  | { kind: "start"; request: StartRequest }
  | { kind: "explain"; reference: DecisionReference }
  | { kind: "action"; actionId: string | undefined }
  | { kind: "queueStatus" }
  | { kind: "toggle"; valid: boolean; enabled: boolean | undefined }
  | { kind: "approve" | "unapprove"; userId: number | undefined };

type DecisionAccess = "public" | "affectedUser" | "administrator";

function positiveId(value: string | undefined): number | undefined {
  if (!value || !/^[1-9]\d*$/.test(value)) return undefined;
  const number = Number(value);
  return Number.isSafeInteger(number) ? number : undefined;
}

function parseCommand(command: Command, botUsername: string): ParsedCommand | undefined {
  if (command.callbackId) {
    const reference = /^undo:([0-9a-f-]+)$/i.exec(command.text)?.[1];
    return {
      kind: "undo",
      decisionId: reference && UUID.test(reference) ? reference.toLowerCase() : undefined,
    };
  }

  const match = /^\/([a-z_]+)(?:@([a-z0-9_]+))?(?:\s+([\s\S]*))?$/i.exec(command.text.trim());
  if (!match) return undefined;
  const [, name, recipient, argumentsText = ""] = match;
  if (recipient && recipient.toLowerCase() !== botUsername.toLowerCase()) return undefined;
  const args = argumentsText.trim().split(/\s+/).filter(Boolean);
  const [argument] = args;
  const commandName = name!.toLowerCase();

  switch (commandName) {
    case "start": {
      if (args.length === 0) return { kind: "start", request: { kind: "history" } };
      if (args.length === 1 && argument!.startsWith("explain_")) {
        const id = argument!.slice("explain_".length);
        const reference: DecisionReference = UUID.test(id)
          ? { kind: "decision", id: id.toLowerCase() }
          : { kind: "unavailable" };
        return { kind: "start", request: { kind: "explain", reference } };
      }
      return { kind: "start", request: { kind: "invalidArguments" } };
    }
    case "explain": {
      if (args.length > 1) return { kind: "explain", reference: { kind: "invalidArguments" } };
      if (argument && UUID.test(argument)) {
        return { kind: "explain", reference: { kind: "decision", id: argument.toLowerCase() } };
      }
      const messageId = argument ? positiveId(argument) : command.replyToMessageId;
      return {
        kind: "explain",
        reference: messageId ? { kind: "message", id: messageId } : { kind: "unavailable" },
      };
    }
    case "action":
      return {
        kind: "action",
        actionId: args.length === 1 && UUID.test(argument!) ? argument!.toLowerCase() : undefined,
      };
    case "queue_status":
      return { kind: "queueStatus" };
    case "toggle_bot": {
      const setting = argument?.toLowerCase();
      const valid =
        args.length === 0 || (args.length === 1 && (setting === "on" || setting === "off"));
      return {
        kind: "toggle",
        valid,
        enabled: setting === undefined ? undefined : setting === "on",
      };
    }
    case "approve":
    case "unapprove": {
      let userId: number | undefined;
      if (args.length === 1) userId = positiveId(argument);
      else if (args.length === 0) userId = command.replyToUserId;
      if (userId === undefined || !Number.isSafeInteger(userId) || userId <= 0) userId = undefined;
      return { kind: commandName, userId };
    }
    default:
      return undefined;
  }
}

function safeText(value: string, settings: Settings): string {
  let text = value.replace(/[\u0000-\u0008\u000b-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, "");
  const secrets = [
    settings.botToken,
    settings.anthropicKey,
    settings.observer?.apiHash,
    settings.observer?.session,
  ];
  for (const secret of secrets) {
    if (secret) text = text.replaceAll(secret, "[redacted]");
  }
  return text;
}

function renderDecision(
  decision: Decision,
  access: DecisionAccess,
  botUsername: string,
  settings: Settings,
): string {
  const verdicts: Record<Decision["verdict"], string> = {
    spam: "Spam detected",
    clean: "No spam detected",
    uncertain: "Inconclusive",
  };
  const lines = [
    `Decision: ${decision.id}`,
    `Result: ${verdicts[decision.verdict]}`,
    `Status: ${decision.status}`,
    `Created: ${new Date(decision.createdAt).toISOString()}`,
    decision.overriddenAt === null
      ? "No administrator override recorded."
      : "An administrator override is recorded.",
  ];
  if (access === "public") {
    lines.push(
      "Evidence and detailed reasons are private. The affected user or a chat administrator can open this bot privately to view details.",
    );
    lines.push(`https://t.me/${botUsername}?start=explain_${decision.id}`);
  } else {
    lines.push(`Reason: ${safeText(decision.reason, settings).slice(0, 2800)}`);
  }
  return lines.join("\n");
}

function renderAction(action: ActionDetail): string {
  // Never include payloads, tokens, attempt errors, or classifier evidence.
  const fields = ["id", "kind", "state", "decisionId", "userId", "attempt"] as const;
  return fields.map((key) => `${key}: ${String(action[key]).slice(0, 150)}`).join("\n");
}

/** Do not persist SDK errors: Telegram request URLs can contain the bot token. */
function commandFailure(error: unknown, signal: AbortSignal): AppError {
  if (signal.aborted) {
    return new AppError({ message: "Command processing interrupted", category: "transient" });
  }
  if (error instanceof AppError) {
    return new AppError({
      message: "Command processing failed",
      category: error.category,
      ...(error.retryAfterMs === undefined ? {} : { retryAfterMs: error.retryAfterMs }),
    });
  }
  const seconds = error instanceof GrammyError ? error.parameters.retry_after : undefined;
  const code = error instanceof GrammyError ? error.error_code : undefined;
  const permanent = typeof code === "number" && code >= 400 && code < 500 && code !== 429;
  return new AppError({
    message: "Telegram command delivery or authorization request failed",
    category: permanent ? "permanent" : "transient",
    ...(typeof seconds === "number" && seconds > 0 ? { retryAfterMs: seconds * 1000 } : {}),
  });
}

/** All mutations are local and transactionally deduplicated; the engine alone performs moderation. */
export async function handleCommand(
  command: Command,
  store: Store,
  telegram: TelegramPort,
  api: Api,
  settings: Settings,
  signal: AbortSignal,
): Promise<void> {
  const privateChat = command.chatId === command.userId;
  const responseChatId = privateChat ? command.chatId : store.resolveChatId(command.chatId);
  const globalAdmin = command.userId === settings.adminId;

  const canAdminister = async (chatId: number): Promise<boolean> => {
    if (!store.tracksChat(chatId)) return false;
    return (
      globalAdmin || (await telegram.isAdmin(store.resolveChatId(chatId), command.userId, signal))
    );
  };
  const inResponseScope = (chatId: number): boolean => {
    return privateChat || store.resolveChatId(chatId) === responseChatId;
  };

  const send = async (text: string, undoDecisionId?: string): Promise<void> => {
    await api.sendMessage(
      responseChatId,
      safeText(text, settings).slice(0, 4000),
      {
        parse_mode: undefined,
        link_preview_options: { is_disabled: true },
        ...(undoDecisionId
          ? {
              reply_markup: {
                inline_keyboard: [
                  [{ text: "Override decision", callback_data: `undo:${undoDecisionId}` }],
                ],
              },
            }
          : {}),
        ...(command.messageId && !command.callbackId && responseChatId === command.chatId
          ? {
              reply_parameters: {
                message_id: command.messageId,
                allow_sending_without_reply: true,
              },
            }
          : {}),
      },
      signal,
    );
  };

  const answerCallback = async (text: string): Promise<void> => {
    if (!command.callbackId) return;
    try {
      await api.answerCallbackQuery(command.callbackId, { text, show_alert: true }, signal);
    } catch (error) {
      // Expired callbacks cannot be answered on durable replay. The mutation remains deduplicated.
      if (!(error instanceof GrammyError) || error.error_code !== 400) throw error;
    }
  };

  const explain = async (reference: DecisionReference): Promise<void> => {
    if (reference.kind === "invalidArguments") {
      await send("Use /explain <decision ID or message ID>, or reply to a message with /explain.");
      return;
    }
    let decision: Decision | undefined;
    if (reference.kind === "decision") decision = store.getDecision(reference.id);
    if (reference.kind === "message" && !privateChat) {
      decision = store.decisionForMessage(command.chatId, reference.id);
    }
    if (!decision || !store.tracksChat(decision.chatId) || !inResponseScope(decision.chatId)) {
      await send("No decision is available for that reference.");
      return;
    }

    let access: DecisionAccess = "public";
    if (privateChat) {
      if (await canAdminister(decision.chatId)) access = "administrator";
      else if (decision.userId === command.userId) access = "affectedUser";
    }
    const undoDecisionId =
      access === "administrator" && decision.overriddenAt === null ? decision.id : undefined;
    await send(renderDecision(decision, access, telegram.botUsername, settings), undoDecisionId);
  };

  try {
    if (
      !Number.isSafeInteger(command.userId) ||
      command.userId <= 0 ||
      command.userId === telegram.botId
    )
      return;
    const parsed = parseCommand(command, telegram.botUsername);
    if (!parsed) return;

    switch (parsed.kind) {
      case "undo": {
        const decision = parsed.decisionId ? store.getDecision(parsed.decisionId) : undefined;
        if (
          !decision ||
          !inResponseScope(decision.chatId) ||
          !(await canAdminister(decision.chatId))
        ) {
          await answerCallback(
            "This action is unavailable or you are not authorized in this chat.",
          );
          return;
        }
        store.applyCommand(command.id, () => store.override(decision.id, command.userId));
        await answerCallback(
          "Override recorded. Any safe compensation is handled by the moderation queue; an unban is not guaranteed.",
        );
        return;
      }
      case "start": {
        if (!privateChat) {
          await send(
            `Open https://t.me/${telegram.botUsername} privately to view your decision history.\n${HELP}`,
          );
          return;
        }
        if (parsed.request.kind === "explain") {
          await explain(parsed.request.reference);
          return;
        }
        if (parsed.request.kind === "invalidArguments") {
          await send(
            "Use /start without arguments for your history, or open a decision explanation link.",
          );
          return;
        }
        const decisions = store
          .decisionsFor(command.userId)
          .filter((decision) => store.tracksChat(decision.chatId))
          .sort((a, b) => b.createdAt - a.createdAt)
          .slice(0, 10);
        if (decisions.length === 0) {
          await send(`No decisions are recorded for you.\n${HELP}`);
          return;
        }
        const history = decisions.map((decision) => {
          const override = decision.overriddenAt === null ? "" : " (overridden)";
          return `${new Date(decision.createdAt).toISOString()} — ${decision.verdict}${override}\n/explain ${decision.id}`;
        });
        await send(
          [
            "Your latest decisions:",
            ...history,
            "Use /explain <decision ID> for the full reason.",
          ].join("\n\n"),
        );
        return;
      }
      case "explain":
        await explain(parsed.reference);
        return;
      case "action": {
        if (!parsed.actionId) {
          await send("Use /action <action ID>.");
          return;
        }
        const action = store.action(parsed.actionId);
        if (!action || !inResponseScope(action.chatId) || !(await canAdminister(action.chatId))) {
          await send("This action is unavailable or you are not authorized in this chat.");
          return;
        }
        await send(renderAction(action));
        return;
      }
      case "queueStatus": {
        const globalView = privateChat && globalAdmin;
        if (!globalView && !(await canAdminister(command.chatId))) {
          await send(
            "This command requires a configured group and its administrator, or the configured global administrator.",
          );
          return;
        }
        const counts = globalView ? store.status() : store.status(command.chatId);
        const text = Object.entries(counts)
          .map(([key, count]) => `${key}: ${count}`)
          .join("\n");
        await send(text || (globalView ? "No queued work." : "No queued work in this chat."));
        return;
      }
      case "toggle":
      case "approve":
      case "unapprove": {
        if (!(await canAdminister(command.chatId))) {
          await send(
            "This command requires a configured group and its administrator, or the configured global administrator.",
          );
          return;
        }
        if (parsed.kind === "toggle") {
          if (!parsed.valid) {
            await send(
              "Use /toggle_bot [on|off]. This controls new classifications, not queued moderation or compensation.",
            );
            return;
          }
          const enabled =
            store.applyCommand(command.id, () =>
              store.toggleChat(command.chatId, parsed.enabled),
            ) ?? store.chatEnabled(command.chatId);
          await send(
            `New classification is ${enabled ? "enabled" : "disabled"} in this chat. Existing moderation and compensation remain queued.`,
          );
          return;
        }
        if (parsed.userId === undefined) {
          await send(
            `Use /${parsed.kind} <numeric user ID> or reply to a user's message. Usernames and channel identities are not accepted.`,
          );
          return;
        }
        const targetId = parsed.userId;
        store.applyCommand(command.id, () => {
          if (parsed.kind === "approve") store.approve(command.chatId, targetId, command.userId);
          else store.unapprove(command.chatId, targetId, command.userId);
        });
        await send(
          parsed.kind === "approve"
            ? `User ${targetId} is explicitly approved in this chat. No Telegram ban or unban was requested.`
            : `Explicit approval removed for user ${targetId} in this chat. Future messages remain subject to classification.`,
        );
        return;
      }
    }
  } catch (error) {
    throw commandFailure(error, signal);
  }
}
