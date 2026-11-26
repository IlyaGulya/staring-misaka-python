/** The public text is also the observer's exact notification-receipt contract. */
export function notificationText(botUsername: string, decisionId: string): string {
  const link = `https://t.me/${botUsername}?start=explain_${decisionId}`;
  return `🚫 Участник забанен за спам. Узнать причину и оспорить — в боте: ${link}`;
}

export const cancelledNotificationText = "✅ Решение отменено администратором.";
