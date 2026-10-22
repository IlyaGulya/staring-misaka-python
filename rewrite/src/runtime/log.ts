import type { Settings } from "../domain.ts";

/** Runtime diagnostics are separate from the transactional moderation audit. */
export class RuntimeLog {
  private readonly secrets: readonly string[];

  constructor(settings: Settings) {
    this.secrets = [
      settings.botToken,
      settings.anthropicKey,
      settings.observer?.session,
      settings.observer?.apiHash,
    ].filter((secret): secret is string => Boolean(secret));
  }

  event(event: string, details: Record<string, unknown> = {}): void {
    console.log(JSON.stringify({ ts: new Date().toISOString(), event, ...details }));
  }

  error(event: string, error: unknown, details: Record<string, unknown> = {}): string {
    let message = error instanceof Error ? error.message : String(error);
    for (const secret of this.secrets) {
      message = message.replaceAll(secret, "[redacted]");
    }
    message = message.replace(/bot\d+:[A-Za-z0-9_-]+/g, "bot[redacted]").slice(0, 1000);
    this.event(event, { ...details, error: message });
    return message;
  }
}
