import { GrammyError, type Api } from "grammy";
import {
  AppError,
  type ActionDetail,
  type Command,
  type Decision,
  type RuntimeStatus,
  type Settings,
  type TelegramPort,
} from "./domain.ts";
import type { Store } from "./store.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const USERNAME = /^@([a-zA-Z0-9_]{3,64})$/;

const HELP = [
  "Команды:",
  "• /start — ваши последние решения",
  "• /explain <ID решения или сообщения> (или reply) — подробности решения",
  "",
  "Для администраторов:",
  "• /status (или /queue_status) — здоровье бота, observer, очередь и расходы",
  "• /recent [N] — последние срабатывания с быстрой отменой",
  "• /approve <user_id | @username> или reply — явно одобрить участника",
  "• /unapprove <user_id | @username> или reply — снять одобрение",
  "• /toggle_bot [on|off] — включить или поставить на паузу новые проверки",
  "• /action <action_id> — диагностика действия",
].join("\n");

type DecisionReference =
  | { kind: "decision"; id: string }
  | { kind: "message"; id: number }
  | { kind: "unavailable" }
  | { kind: "invalidArguments" };

type StartRequest =
  | { kind: "history" }
  | { kind: "explain"; reference: DecisionReference }
  | { kind: "invalidArguments" };

type UserTarget =
  { kind: "id"; userId: number } | { kind: "username"; username: string } | { kind: "missing" };

type ParsedCommand =
  | { kind: "undo"; decisionId: string | undefined }
  | { kind: "approveCallback"; chatId: number | undefined; userId: number | undefined }
  | { kind: "start"; request: StartRequest }
  | { kind: "help" }
  | { kind: "explain"; reference: DecisionReference }
  | { kind: "action"; actionId: string | undefined }
  | { kind: "queueStatus" }
  | { kind: "recent"; limit: number }
  | { kind: "toggle"; valid: boolean; enabled: boolean | undefined }
  | { kind: "approve" | "unapprove"; target: UserTarget };

type DecisionAccess = "public" | "affectedUser" | "administrator";

function positiveId(value: string | undefined): number | undefined {
  if (!value || !/^[1-9]\d*$/.test(value)) return undefined;
  const number = Number(value);
  return Number.isSafeInteger(number) ? number : undefined;
}

function signedId(value: string | undefined): number | undefined {
  if (!value || !/^-?\d+$/.test(value)) return undefined;
  const number = Number(value);
  return Number.isSafeInteger(number) ? number : undefined;
}

function parseCommand(command: Command, botUsername: string): ParsedCommand | undefined {
  if (command.callbackId) {
    const undo = /^undo:([0-9a-f-]+)$/i.exec(command.text)?.[1];
    if (undo !== undefined || command.text.startsWith("undo:")) {
      return {
        kind: "undo",
        decisionId: undo && UUID.test(undo) ? undo.toLowerCase() : undefined,
      };
    }
    const approveMatch = /^approve:(-?\d+):([1-9]\d*)$/.exec(command.text);
    if (approveMatch || command.text.startsWith("approve:")) {
      return {
        kind: "approveCallback",
        chatId: approveMatch ? signedId(approveMatch[1]) : undefined,
        userId: approveMatch ? positiveId(approveMatch[2]) : undefined,
      };
    }
    return undefined;
  }

  const match = /^\/([a-z_]+)(?:@([a-z0-9_]+))?(?:\s+([\s\S]*))?$/i.exec(command.text.trim());
  if (!match) return undefined;
  const [, name, recipient, argumentsText = ""] = match;
  if (recipient && recipient.toLowerCase() !== botUsername.toLowerCase()) return undefined;
  const args = argumentsText.trim().split(/\s+/).filter(Boolean);
  const [argument] = args;
  const commandName = name!.toLowerCase();

  switch (commandName) {
    case "help":
      return { kind: "help" };
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
    case "status":
    case "queue_status":
      return { kind: "queueStatus" };
    case "recent": {
      const parsedLimit = argument ? positiveId(argument) : 5;
      return { kind: "recent", limit: Math.min(10, Math.max(1, parsedLimit ?? 5)) };
    }
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
      if (args.length === 1) {
        const id = positiveId(argument);
        if (id !== undefined) return { kind: commandName, target: { kind: "id", userId: id } };
        const handle = argument ? USERNAME.exec(argument)?.[1] : undefined;
        if (handle) return { kind: commandName, target: { kind: "username", username: handle } };
        return { kind: commandName, target: { kind: "missing" } };
      }
      if (args.length === 0 && command.replyToUserId && command.replyToUserId > 0) {
        return { kind: commandName, target: { kind: "id", userId: command.replyToUserId } };
      }
      return { kind: commandName, target: { kind: "missing" } };
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
    settings.openaiKey,
    settings.observer?.apiHash,
    settings.observer?.session,
  ];
  for (const secret of secrets) {
    if (secret) text = text.replaceAll(secret, "[redacted]");
  }
  return text.replace(/bot\d+:[A-Za-z0-9_-]+/g, "bot[redacted]");
}

function chatLabel(settings: Settings, chatId: number): string {
  const name = settings.chatNames?.[String(chatId)];
  return name ? `${name} (${chatId})` : `Чат ${chatId}`;
}

function renderDecision(
  decision: Decision,
  access: DecisionAccess,
  botUsername: string,
  settings: Settings,
): string {
  const verdicts: Record<Decision["verdict"], string> = {
    spam: "🚫 Обнаружен спам",
    clean: "✅ Спам не обнаружен",
    uncertain: "❓ Недостаточно данных",
  };
  const lines = [
    `Решение: ${decision.id}`,
    `Чат: ${chatLabel(settings, decision.chatId)}`,
    `Вердикт: ${verdicts[decision.verdict]}`,
    `Статус: ${decision.status}`,
    `Время: ${new Date(decision.createdAt).toISOString()}`,
    decision.overriddenAt === null
      ? "Отмена администратором: нет"
      : "Отмена администратором: зафиксирована",
  ];
  if (access === "public") {
    lines.push(
      "Подробная причина скрыта в общем чате. Участник или администратор могут открыть её в личных сообщениях с ботом:",
    );
    lines.push(`https://t.me/${botUsername}?start=explain_${decision.id}`);
  } else {
    if (access === "administrator") {
      lines.push(`Пользователь: ${decision.userId}`);
      lines.push(`Модель: ${decision.model}`);
    }
    lines.push(`Причина: ${safeText(decision.reason, settings).slice(0, 2800)}`);
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

type InlineRow = Array<{ text: string; callback_data: string }>;

/** All mutations are local and transactionally deduplicated; the engine alone performs moderation. */
export async function handleCommand(
  command: Command,
  store: Store,
  telegram: TelegramPort,
  api: Api,
  settings: Settings,
  signal: AbortSignal,
  runtimeStatus?: RuntimeStatus,
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

  const send = async (text: string, keyboard?: InlineRow[]): Promise<void> => {
    await api.sendMessage(
      responseChatId,
      safeText(text, settings).slice(0, 4000),
      {
        parse_mode: undefined,
        link_preview_options: { is_disabled: true },
        ...(keyboard && keyboard.length > 0 ? { reply_markup: { inline_keyboard: keyboard } } : {}),
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
      await send(
        "Используйте /explain <ID решения или ID сообщения> или ответьте на сообщение командой /explain.",
      );
      return;
    }
    let decision: Decision | undefined;
    if (reference.kind === "decision") decision = store.getDecision(reference.id);
    if (reference.kind === "message" && !privateChat) {
      decision = store.decisionForMessage(command.chatId, reference.id);
    }
    if (!decision || !store.tracksChat(decision.chatId) || !inResponseScope(decision.chatId)) {
      await send("Решение по этой ссылке не найдено.");
      return;
    }

    let access: DecisionAccess = "public";
    if (privateChat) {
      if (await canAdminister(decision.chatId)) access = "administrator";
      else if (decision.userId === command.userId) access = "affectedUser";
    }
    const keyboard: InlineRow[] = [];
    if (access === "administrator" && decision.overriddenAt === null) {
      keyboard.push([
        { text: "↩️ Отменить решение", callback_data: `undo:${decision.id}` },
        {
          text: "✅ Одобрить участника",
          callback_data: `approve:${decision.chatId}:${decision.userId}`,
        },
      ]);
    }
    await send(renderDecision(decision, access, telegram.botUsername, settings), keyboard);
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
      case "help": {
        await send(HELP);
        return;
      }
      case "undo": {
        const decision = parsed.decisionId ? store.getDecision(parsed.decisionId) : undefined;
        if (
          !decision ||
          !inResponseScope(decision.chatId) ||
          !(await canAdminister(decision.chatId))
        ) {
          await answerCallback(
            "Действие недоступно или у вас нет прав администратора в этом чате.",
          );
          return;
        }
        store.applyCommand(command.id, () => store.override(decision.id, command.userId));
        await answerCallback(
          "Отмена записана. Если бан выполнял бот, разбан поставлен в очередь. Удалённые сообщения восстановить нельзя.",
        );
        return;
      }
      case "approveCallback": {
        const { chatId, userId } = parsed;
        if (
          chatId === undefined ||
          userId === undefined ||
          !inResponseScope(chatId) ||
          !(await canAdminister(chatId))
        ) {
          await answerCallback(
            "Действие недоступно или у вас нет прав администратора в этом чате.",
          );
          return;
        }
        store.applyCommand(command.id, () => store.approve(chatId, userId, command.userId));
        await answerCallback(
          `Пользователь ${userId} одобрен в чате. Активный бан бота (если был) поставлен на отмену.`,
        );
        return;
      }
      case "start": {
        if (!privateChat) {
          await send(
            `Откройте https://t.me/${telegram.botUsername} в личных сообщениях, чтобы посмотреть историю решений.\n\n${HELP}`,
          );
          return;
        }
        if (parsed.request.kind === "explain") {
          await explain(parsed.request.reference);
          return;
        }
        if (parsed.request.kind === "invalidArguments") {
          await send(
            "Используйте /start без аргументов для просмотра истории или откройте ссылку из уведомления.",
          );
          return;
        }
        const decisions = store
          .decisionsFor(command.userId)
          .filter((decision) => store.tracksChat(decision.chatId))
          .sort((a, b) => b.createdAt - a.createdAt)
          .slice(0, 10);
        if (decisions.length === 0) {
          await send(`Для вас пока нет записанных решений.\n\n${HELP}`);
          return;
        }
        const history = decisions.map((decision) => {
          const override = decision.overriddenAt === null ? "" : " (отменено)";
          return `${new Date(decision.createdAt).toISOString()} — ${decision.verdict}${override}\n/explain ${decision.id}`;
        });
        await send(
          [
            "Ваши последние решения:",
            ...history,
            "Отправьте /explain <ID решения>, чтобы увидеть подробности.",
          ].join("\n\n"),
        );
        return;
      }
      case "explain":
        await explain(parsed.reference);
        return;
      case "action": {
        if (!parsed.actionId) {
          await send("Используйте /action <ID действия>.");
          return;
        }
        const action = store.action(parsed.actionId);
        if (!action || !inResponseScope(action.chatId) || !(await canAdminister(action.chatId))) {
          await send("Действие не найдено или у вас нет прав администратора в этом чате.");
          return;
        }
        await send(renderAction(action));
        return;
      }
      case "queueStatus": {
        const globalView = privateChat && globalAdmin;
        if (!globalView && !(await canAdminister(command.chatId))) {
          await send(
            "Команда доступна только администратору отслеживаемого чата или глобальному администратору в ЛС.",
          );
          return;
        }
        const counts = globalView ? store.status() : store.status(command.chatId);
        const lines: string[] = [];
        const provider = settings.provider ?? "anthropic";
        const modelLabel =
          provider === "openai"
            ? `${provider} / ${settings.model} (${settings.reasoningEffort ?? "low"})`
            : `${provider} / ${settings.model}`;
        lines.push(`📊 Статус Misaka (${settings.mode.toUpperCase()})`, `Модель: ${modelLabel}`);
        if (runtimeStatus) {
          const obs = !runtimeStatus.observerConfigured
            ? "не настроен"
            : runtimeStatus.observerConnected
              ? "подключён ✅"
              : "отключён ⚠️";
          const uptimeMin = Math.floor((Date.now() - runtimeStatus.startedAt) / 60_000);
          lines.push(`Observer: ${obs} | Аптайм: ${uptimeMin} мин`);
        }
        if (typeof store.todayUsage === "function") {
          const usage = globalView ? store.todayUsage() : store.todayUsage(command.chatId);
          const cap = settings.dailyBudgetUsd ?? 0;
          const capText = cap > 0 ? ` / $${cap.toFixed(2)}` : "";
          lines.push(
            `Расход за ${usage.day}: ~$${usage.estimatedUsd.toFixed(4)}${capText} (${usage.requests} запр., ${usage.inputTokens + usage.outputTokens} ток.)`,
          );
        }
        const counters = Object.entries(counts)
          .map(([key, count]) => `• ${key}: ${count}`)
          .join("\n");
        if (counters) {
          lines.push("", "Очередь и состояние:", counters);
        }
        if (globalView && typeof store.chatSummaries === "function") {
          const chats = store.chatSummaries();
          if (chats.length > 0) {
            lines.push(
              "",
              "Чаты:",
              ...chats.map(
                (chat) =>
                  `• ${chat.name}: ${chat.enabled ? "вкл ✅" : "пауза ⏸"} (одобрено: ${chat.approved}, старожилов: ${chat.regulars})`,
              ),
            );
          }
        }
        await send(lines.join("\n"));
        return;
      }
      case "recent": {
        const globalView = privateChat && globalAdmin;
        if (!globalView && !(await canAdminister(command.chatId))) {
          await send(
            "Команда доступна только администратору отслеживаемого чата или глобальному администратору в ЛС.",
          );
          return;
        }
        const items =
          typeof store.recentDecisions === "function"
            ? store.recentDecisions(parsed.limit, globalView ? undefined : command.chatId)
            : [];
        if (items.length === 0) {
          await send("Недавних срабатываний спама или спорных решений нет.");
          return;
        }
        const lines = ["🕘 Последние срабатывания:"];
        const keyboard: InlineRow[] = [];
        for (const [index, item] of items.entries()) {
          const tag = item.overriddenAt
            ? "↩️ Отменено"
            : item.verdict === "spam"
              ? item.mode === "live"
                ? "🚫 Спам (LIVE)"
                : "👁 Спам (SHADOW)"
              : "❓ Неясно";
          const who = [
            item.displayName,
            item.username ? `@${item.username}` : null,
            `ID ${item.userId}`,
          ]
            .filter(Boolean)
            .join(" · ");
          lines.push(
            `\n${index + 1}. ${tag} — ${chatLabel(settings, item.chatId)}`,
            `   Кто: ${who}`,
            `   Причина: ${safeText(item.reason, settings).slice(0, 180)}`,
          );
          if (privateChat && item.snippet) {
            lines.push(`   Текст: ${safeText(item.snippet, settings).slice(0, 140)}`);
          }
          lines.push(`   /explain ${item.id}`);
          if (privateChat && item.overriddenAt === null && item.verdict === "spam") {
            keyboard.push([
              {
                text: `↩️ Отменить #${index + 1} (${item.userId})`,
                callback_data: `undo:${item.id}`,
              },
            ]);
          }
        }
        await send(lines.join("\n"), keyboard);
        return;
      }
      case "toggle":
      case "approve":
      case "unapprove": {
        if (!(await canAdminister(command.chatId))) {
          await send(
            "Команда доступна только администратору отслеживаемого чата или глобальному администратору.",
          );
          return;
        }
        if (parsed.kind === "toggle") {
          if (!parsed.valid) {
            await send(
              "Используйте /toggle_bot [on|off]. Переключает новые проверки, не отменяя уже поставленные действия.",
            );
            return;
          }
          const enabled =
            store.applyCommand(command.id, () =>
              store.toggleChat(command.chatId, parsed.enabled),
            ) ?? store.chatEnabled(command.chatId);
          await send(
            enabled
              ? "✅ Проверка новых сообщений в этом чате включена."
              : "⏸ Проверка новых сообщений в этом чате поставлена на паузу. Уже принятые действия и отмены продолжают выполняться.",
          );
          return;
        }
        let targetId: number | undefined;
        if (parsed.target.kind === "id") {
          targetId = parsed.target.userId;
        } else if (parsed.target.kind === "username") {
          const matches =
            typeof store.resolveUsername === "function"
              ? store.resolveUsername(command.chatId, parsed.target.username)
              : [];
          if (matches.length === 1) {
            targetId = matches[0];
          } else if (matches.length > 1) {
            await send(
              `Найдено несколько пользователей с @${parsed.target.username} в истории этого чата: ${matches.join(", ")}. Укажите точный числовой ID.`,
            );
            return;
          } else {
            await send(
              `Пользователь @${parsed.target.username} не встречался в этом чате. Укажите числовой user ID или ответьте на его сообщение.`,
            );
            return;
          }
        }
        if (targetId === undefined) {
          await send(
            `Используйте /${parsed.kind} <числовой user ID | @username> или ответьте на сообщение участника.`,
          );
          return;
        }
        const resolvedTargetId = targetId;
        store.applyCommand(command.id, () => {
          if (parsed.kind === "approve") {
            store.approve(command.chatId, resolvedTargetId, command.userId);
          } else {
            store.unapprove(command.chatId, resolvedTargetId, command.userId);
          }
        });
        await send(
          parsed.kind === "approve"
            ? `✅ Пользователь ${resolvedTargetId} явно одобрен в этом чате.`
            : `🔄 Одобрение снято для пользователя ${resolvedTargetId}. Новые сообщения снова будут проверяться.`,
        );
        return;
      }
    }
  } catch (error) {
    throw commandFailure(error, signal);
  }
}
