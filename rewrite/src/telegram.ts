import { Api, GrammyError } from "grammy";
import { AppError, type Settings, type TelegramPort } from "./domain";

export function telegramError(error: unknown, mutation = false): AppError {
  if (error instanceof AppError) return error;
  if (error instanceof GrammyError) {
    let category: AppError["category"] = "permanent";
    if (error.error_code === 429) category = "transient";
    else if (error.error_code >= 500) category = mutation ? "unknown" : "transient";

    const retryAfter = error.parameters.retry_after;
    return new AppError({
      message: `Telegram ${error.method}: ${error.error_code}`,
      category,
      ...(retryAfter === undefined ? {} : { retryAfterMs: retryAfter * 1000 }),
    });
  }
  // A disconnected request may already have committed. Do not persist token-bearing URLs.
  return new AppError({
    message: "Telegram transport failed",
    category: mutation ? "unknown" : "transient",
  });
}

export async function createTelegram(
  settings: Settings,
  signal?: AbortSignal,
): Promise<{ port: TelegramPort; api: Api }> {
  const api = new Api(settings.botToken, {
    apiRoot: settings.telegramApiRoot,
    timeoutSeconds: settings.pollTimeout + 15,
  });
  async function call<T>(
    mutation: boolean,
    signal: AbortSignal,
    operation: () => Promise<T>,
  ): Promise<T> {
    if (signal.aborted) {
      throw new AppError({
        message: "Telegram request cancelled before dispatch",
        category: "transient",
      });
    }
    try {
      return await operation();
    } catch (error) {
      throw telegramError(error, mutation);
    }
  }

  const startupTimeout = AbortSignal.timeout(30_000);
  const startupSignal = signal ? AbortSignal.any([signal, startupTimeout]) : startupTimeout;
  const me = await api.getMe(startupSignal).catch((error: unknown) => {
    throw telegramError(error);
  });
  const port: TelegramPort = {
    botId: me.id,
    botUsername: me.username,
    async ban(chatId, userId, signal) {
      await call(true, signal, () =>
        api.banChatMember(chatId, userId, { revoke_messages: false }, signal),
      );
    },
    async unban(chatId, userId, signal) {
      await call(true, signal, () =>
        api.unbanChatMember(chatId, userId, { only_if_banned: true }, signal),
      );
    },
    async membership(chatId, userId, signal) {
      const member = await call(false, signal, () => api.getChatMember(chatId, userId, signal));
      return {
        restricted: member.status === "kicked" || member.status === "restricted",
        isMember:
          member.status === "restricted"
            ? member.is_member
            : member.status !== "left" && member.status !== "kicked",
      };
    },
    async delete(chatId, messageId, signal) {
      await call(true, signal, async () => {
        try {
          await api.deleteMessage(chatId, messageId, signal);
        } catch (error) {
          const alreadyDeleted =
            error instanceof GrammyError &&
            error.error_code === 400 &&
            /(?:^|: )message to delete not found$/i.test(error.description);
          if (!alreadyDeleted) throw error;
        }
      });
    },
    async edit(chatId, messageId, text, signal) {
      await call(true, signal, async () => {
        try {
          await api.editMessageText(
            chatId,
            messageId,
            text,
            {
              link_preview_options: { is_disabled: true },
            },
            signal,
          );
        } catch (error) {
          const alreadyEdited =
            error instanceof GrammyError &&
            error.error_code === 400 &&
            /(?:^|: )message is not modified(?:$|:)/i.test(error.description);
          if (!alreadyEdited) throw error;
        }
      });
    },
    async notify(chatId, text, signal) {
      const message = await call(true, signal, () =>
        api.sendMessage(
          chatId,
          text,
          {
            link_preview_options: { is_disabled: true },
          },
          signal,
        ),
      );
      return message.message_id;
    },
    async isAdmin(chatId, userId, signal) {
      const member = await call(false, signal, () => api.getChatMember(chatId, userId, signal));
      return member.status === "creator" || member.status === "administrator";
    },
  };
  return { api, port };
}
