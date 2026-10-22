import { createInterface } from "node:readline/promises";
import { Writable } from "node:stream";
import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions";
import { Logger, LogLevel } from "telegram/extensions/Logger";

/** Only this explicitly invoked CLI may authenticate a user or export a session. */
export async function observerLogin(): Promise<void> {
  if (process.argv.length !== 3 || process.argv[2] !== "--export-session") {
    throw new Error(
      "Usage: bun run src/observer-login.ts --export-session (session secret is written to stdout)",
    );
  }
  if (!process.stdin.isTTY || !process.stderr.isTTY) {
    throw new Error("Observer login requires an interactive terminal");
  }
  const apiId = Number(process.env.OBSERVER_API_ID);
  const apiHash = process.env.OBSERVER_API_HASH;
  if (!Number.isSafeInteger(apiId) || apiId <= 0 || !apiHash) {
    throw new Error("Set OBSERVER_API_ID and OBSERVER_API_HASH before login");
  }
  process.stderr.write(
    "This logs in a Telegram USER account. The exported session grants access to that account. Store it only in your secret manager, never in this repository. Input is hidden.\n",
  );
  // Keep prompts off stdout so an intentional pipe to a secret manager receives only the session.
  const output = new Writable({
    write(_chunk, _encoding, callback) {
      callback();
    },
  });
  const terminal = createInterface({ input: process.stdin, output, terminal: true });
  const ask = async (prompt: string): Promise<string> => {
    process.stderr.write(prompt);
    const value = await terminal.question("");
    process.stderr.write("\n");
    if (!value) throw new Error("AUTH_USER_CANCEL");
    return value;
  };
  const session = new StringSession("");
  const client = new TelegramClient(session, apiId, apiHash, {
    baseLogger: new Logger(LogLevel.NONE),
    connectionRetries: 3,
    requestRetries: 2,
    autoReconnect: false,
  });
  try {
    await client.start({
      phoneNumber: () => ask("Phone number (international format): "),
      phoneCode: () => ask("Telegram login code: "),
      password: () => ask("Two-factor password: "),
      onError: async () => {
        process.stderr.write("Authentication failed; login stopped.\n");
        return true;
      },
    });
    if (!(await client.checkAuthorization())) throw new Error("Authentication did not complete");
    const me = await client.getMe();
    if (me.bot) throw new Error("Observer requires a user session, not a bot session");
    process.stdout.write(`${session.save()}\n`);
  } finally {
    terminal.close();
    output.destroy();
    await client.destroy();
  }
}

if (import.meta.main) {
  observerLogin().catch((error) => {
    // SDK errors may contain authentication details. Only our explicit setup errors are printable.
    const message = error instanceof Error ? error.message : "";
    const safeToPrint =
      message.startsWith("Usage:") ||
      message.startsWith("Observer ") ||
      message.startsWith("Set OBSERVER_");
    process.stderr.write(
      `${safeToPrint ? message : "Observer login failed; no session exported"}\n`,
    );
    process.exitCode = 1;
  });
}
