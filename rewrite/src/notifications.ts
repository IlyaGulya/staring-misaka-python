/** The public text is also the observer's exact notification-receipt contract. */
export function notificationText(botUsername: string, decisionId: string): string {
  const link = `https://t.me/${botUsername}?start=explain_${decisionId}`;
  return `Moderation decision recorded. Details and appeal: ${link}`;
}

export const cancelledNotificationText = "Решение отменено";
