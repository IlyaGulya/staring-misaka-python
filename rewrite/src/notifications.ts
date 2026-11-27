import type { Settings } from './domain.ts'

/** The public text is also the observer's exact notification-receipt contract. */
export function notificationText(botUsername: string, decisionId: string): string {
  const link = `https://t.me/${botUsername}?start=explain_${decisionId}`
  return `🚫 Участник забанен за спам. Узнать причину и оспорить — в боте: ${link}`
}

export const cancelledNotificationText = '✅ Решение отменено администратором.'

export function sanitizeNotificationText(value: string, settings: Settings): string {
  // oxlint-disable-next-line no-control-regex -- Preserve tab and line feed while removing other C0 controls.
  let text = value.replace(/[\u0000-\u0008\u000b-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, '')
  for (const secret of [
    settings.botToken,
    settings.anthropicKey,
    settings.openaiKey,
    settings.observer?.apiHash,
    settings.observer?.session,
  ]) {
    if (secret) text = text.replaceAll(secret, '[redacted]')
  }
  return text.replace(/bot\d+:[A-Za-z0-9_-]+/g, 'bot[redacted]')
}
