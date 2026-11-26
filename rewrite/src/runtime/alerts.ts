import { GrammyError, type Api } from "grammy";
import type { AdminBanNotice, RuntimeStatus, Settings } from "../domain.ts";
import type { Store } from "../store.ts";
import type { RuntimeLog } from "./log.ts";

const OBSERVER_DOWN_GRACE_MS = 5 * 60 * 1000;

function sanitize(value: string, settings: Settings): string {
  let text = value.replace(/[\u0000-\u0008\u000b-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, "");
  for (const secret of [
    settings.botToken,
    settings.anthropicKey,
    settings.openaiKey,
    settings.observer?.apiHash,
    settings.observer?.session,
  ]) {
    if (secret) text = text.replaceAll(secret, "[redacted]");
  }
  return text.replace(/bot\d+:[A-Za-z0-9_-]+/g, "bot[redacted]");
}

function chatLabel(settings: Settings, chatId: number): string {
  const name = settings.chatNames?.[String(chatId)];
  return name ? `${name} (${chatId})` : `Чат ${chatId}`;
}

function formatUser(notice: AdminBanNotice): string {
  const parts: string[] = [];
  if (notice.displayName) parts.push(notice.displayName);
  if (notice.username) parts.push(`@${notice.username}`);
  parts.push(`ID ${notice.userId}`);
  return parts.join(" · ");
}

export function renderAdminBanCard(notice: AdminBanNotice, settings: Settings): string {
  const modeTag = notice.mode === "live" ? "🚫 Спам-бан (LIVE)" : "👁 Теневой спам (SHADOW)";
  const lines = [
    modeTag,
    `Чат: ${chatLabel(settings, notice.chatId)}`,
    `Пользователь: ${sanitize(formatUser(notice), settings)}`,
    `Модель: ${notice.model}`,
    `Причина: ${sanitize(notice.reason, settings).slice(0, 800)}`,
  ];
  if (notice.snippet) {
    lines.push(`Фрагмент: ${sanitize(notice.snippet, settings).slice(0, 240)}`);
  }
  lines.push(`Решение: ${notice.decisionId}`);
  return lines.join("\n");
}

/** Deliver durable admin ban summaries and deduplicated operational incident alerts to ADMIN_ID DM. */
export async function runAdminAlertCycle(
  store: Store,
  api: Api,
  settings: Settings,
  status: RuntimeStatus,
  log: RuntimeLog,
  signal: AbortSignal,
): Promise<void> {
  if (settings.adminBanNotifications !== false) {
    const notice = store.claimAdminBanNotice();
    if (notice) {
      try {
        const sent = await api.sendMessage(
          settings.adminId,
          renderAdminBanCard(notice, settings).slice(0, 4000),
          {
            disable_notification: true,
            link_preview_options: { is_disabled: true },
            reply_markup: {
              inline_keyboard: [
                [
                  { text: "↩️ Отменить решение", callback_data: `undo:${notice.decisionId}` },
                  {
                    text: "✅ Одобрить",
                    callback_data: `approve:${notice.chatId}:${notice.userId}`,
                  },
                ],
              ],
            },
          },
          signal,
        );
        store.completeAdminBanNotice(notice.decisionId, sent.message_id);
      } catch (error) {
        const message = log.error("admin_ban_notice_failed", error, {
          decisionId: notice.decisionId,
        });
        const permanent =
          error instanceof GrammyError &&
          error.error_code >= 400 &&
          error.error_code < 500 &&
          error.error_code !== 429;
        store.failAdminBanNotice(notice.decisionId, message, permanent);
      }
    }
  }

  if (settings.adminAlerts === false) return;

  const checks: Array<{ key: string; active: boolean; openText: string; resolvedText: string }> =
    [];

  // 1. Daily LLM budget guard
  const budgetExceeded = store.isBudgetExceeded();
  const usage = store.todayUsage();
  checks.push({
    key: "budget_exceeded",
    active: budgetExceeded,
    openText: `⚠️ Достигнут дневной лимит LLM-расходов: $${usage.estimatedUsd.toFixed(4)} / $${(settings.dailyBudgetUsd ?? 0).toFixed(2)} (${usage.requests} запросов за ${usage.day}). Новые классификации поставлены на паузу до завтра (UTC).`,
    resolvedText: `✅ Дневной лимит LLM-расходов снова в норме ($${usage.estimatedUsd.toFixed(4)}). Классификация возобновлена.`,
  });

  // 2. LLM auth / permanent credential failure
  const authBlocked = store.blockedAuthJobs();
  checks.push({
    key: "llm_auth",
    active: authBlocked > 0,
    openText: `🚨 Ошибка авторизации LLM (${settings.provider ?? "anthropic"}): заблокировано задач — ${authBlocked}. Проверьте API-ключ и квоту.`,
    resolvedText: `✅ Авторизация LLM восстановлена, заблокированных задач нет.`,
  });

  // 3. Observer disconnected for > 5 minutes
  const now = Date.now();
  const observerStale =
    status.observerConfigured &&
    !status.observerConnected &&
    status.observerDisconnectedSince !== null &&
    now - status.observerDisconnectedSince >= OBSERVER_DOWN_GRACE_MS;
  checks.push({
    key: "observer_down",
    active: observerStale,
    openText: `⚠️ MTProto-наблюдатель (observer) отключён более 5 минут. Основной бот продолжает работу по Bot API, но события удалений и сообщений других ботов временно не поступают.`,
    resolvedText: `✅ MTProto-наблюдатель (observer) снова подключён.`,
  });

  // 4. Actions requiring manual attention
  const attention = store.needsAttentionActions();
  const activeAttentionKeys = new Set<string>();
  for (const item of attention) {
    const key = `needs_attention:${item.id}`;
    activeAttentionKeys.add(key);
    checks.push({
      key,
      active: true,
      openText: `⚠️ Действие требует внимания администратора.\nЧат: ${chatLabel(settings, item.chatId)}\nПользователь: ${item.userId}\nПодробности: /action ${item.id}`,
      resolvedText: `✅ Действие ${item.id} больше не требует внимания.`,
    });
  }
  for (const existing of store.openIncidents()) {
    if (existing.key.startsWith("needs_attention:") && !activeAttentionKeys.has(existing.key)) {
      checks.push({
        key: existing.key,
        active: false,
        openText: "",
        resolvedText: `✅ Инцидент ${existing.key.slice("needs_attention:".length)} закрыт.`,
      });
    }
  }

  for (const check of checks) {
    const transition = store.recordIncident(
      check.key,
      check.active,
      check.active ? check.openText : check.resolvedText,
    );
    if (transition.notify === "none") continue;
    const text = transition.notify === "opened" ? check.openText : check.resolvedText;
    try {
      await api.sendMessage(
        settings.adminId,
        sanitize(text, settings).slice(0, 4000),
        { link_preview_options: { is_disabled: true } },
        signal,
      );
    } catch (error) {
      log.error("admin_alert_send_failed", error, { incidentKey: check.key });
    }
  }
}
