import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Schema } from "effect";
import type { Settings } from "./domain.ts";

const SpamConfig = Schema.Struct({
  system_prompt: Schema.String,
  model: Schema.optional(Schema.String),
  chats: Schema.optional(
    Schema.Record(
      Schema.String,
      Schema.Struct({
        system_prompt: Schema.optional(Schema.String),
        extra_instructions: Schema.optional(Schema.String),
      }),
    ),
  ),
});

function integer(value: string | undefined, name: string, fallback?: number): number {
  if (value === undefined || value === "") {
    if (fallback !== undefined) return fallback;
    throw new Error(`${name} is required`);
  }
  const number = Number(value);
  if (!/^-?\d+$/.test(value) || !Number.isSafeInteger(number)) {
    throw new Error(`${name} must be a safe integer`);
  }
  return number;
}

function positive(value: string | undefined, name: string, fallback: number): number {
  const number = integer(value, name, fallback);
  if (number <= 0) throw new Error(`${name} must be positive`);
  return number;
}

function endpoint(value: string, name: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${name} must be a valid URL`);
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error(`${name} must not contain credentials, query or fragment`);
  }
  if (
    url.protocol !== "https:" &&
    !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))
  ) {
    throw new Error(
      `${name} requires HTTPS (HTTP is allowed only on loopback for local verification)`,
    );
  }
  return value.replace(/\/$/, "");
}

export function loadSettings(
  env: Record<string, string | undefined> = process.env,
  requireSecrets = true,
): Settings {
  const file = resolve(env.SPAM_CONFIG_PATH ?? "../config.yaml");
  let yaml: typeof SpamConfig.Type;
  try {
    const contents = readFileSync(file, "utf8");
    yaml = Schema.decodeUnknownSync(SpamConfig)(Bun.YAML.parse(contents));
  } catch {
    // Parser errors can include source contents. Do not expose configuration secrets.
    throw new Error(
      "Invalid spam YAML: expected system_prompt, optional model and per-chat prompts",
    );
  }
  if (!yaml.system_prompt.trim()) throw new Error("system_prompt is required in spam YAML");
  const chats = yaml.chats ?? {};
  const configuredIds = env.TRACKING_CHAT_IDS?.trim() || Object.keys(chats).join(",");
  const chatIds = configuredIds
    .split(",")
    .filter(Boolean)
    .map((value) => integer(value.trim(), "TRACKING_CHAT_IDS"));
  if (!chatIds.length || chatIds.some((id) => id >= 0)) {
    throw new Error("TRACKING_CHAT_IDS must contain group/channel IDs");
  }
  const chatPrompts: Record<string, string> = {};
  for (const [id, entry] of Object.entries(chats)) {
    const system = entry.system_prompt ?? yaml.system_prompt;
    chatPrompts[id] = system + (entry.extra_instructions ? `\n\n${entry.extra_instructions}` : "");
  }
  const mode = env.MODE ?? "shadow";
  if (mode !== "shadow" && mode !== "live") throw new Error("MODE must be shadow or live");
  if (requireSecrets && (!env.BOT_TOKEN || !env.ANTHROPIC_API_KEY)) {
    throw new Error("BOT_TOKEN and ANTHROPIC_API_KEY are required");
  }
  const adminId = integer(env.ADMIN_ID, "ADMIN_ID", requireSecrets ? undefined : 1);
  if (adminId <= 0) throw new Error("ADMIN_ID must be a positive user ID");
  const observerKeys = [env.OBSERVER_API_ID, env.OBSERVER_API_HASH, env.OBSERVER_SESSION];
  const configuredObserver = observerKeys.some(Boolean);
  if (configuredObserver && !observerKeys.every(Boolean)) {
    throw new Error("OBSERVER_API_ID, OBSERVER_API_HASH and OBSERVER_SESSION must be set together");
  }
  if (requireSecrets && mode === "live") {
    if (env.ALLOW_LIVE_MODERATION !== "true") {
      throw new Error("Live mode requires ALLOW_LIVE_MODERATION=true");
    }
    if (!configuredObserver && env.ACKNOWLEDGE_OBSERVER_GAP !== "true") {
      throw new Error("Live mode requires an observer or explicit ACKNOWLEDGE_OBSERVER_GAP=true");
    }
  }
  const dbPath = resolve(env.DB_PATH ?? "./data/misaka.sqlite");
  return {
    mode,
    dbPath,
    botToken: env.BOT_TOKEN ?? "",
    adminId,
    chatIds: [...new Set(chatIds)],
    anthropicKey: env.ANTHROPIC_API_KEY ?? "",
    anthropicUrl: endpoint(
      env.ANTHROPIC_BASE_URL ?? "https://api.anthropic.com",
      "ANTHROPIC_BASE_URL",
    ),
    model: env.LLM_MODEL?.trim() || yaml.model || "claude-sonnet-4-5-20250929",
    systemPrompt: yaml.system_prompt,
    chatPrompts,
    telegramApiRoot: endpoint(
      env.TELEGRAM_API_ROOT ?? "https://api.telegram.org",
      "TELEGRAM_API_ROOT",
    ),
    pollTimeout: positive(env.POLL_TIMEOUT, "POLL_TIMEOUT", 25),
    concurrency: positive(env.CLASSIFICATION_CONCURRENCY, "CLASSIFICATION_CONCURRENCY", 3),
    followupTtlMs: positive(env.FOLLOWUP_TTL_SECONDS, "FOLLOWUP_TTL_SECONDS", 60) * 1000,
    classificationTimeoutMs:
      positive(env.CLASSIFICATION_TIMEOUT_SECONDS, "CLASSIFICATION_TIMEOUT_SECONDS", 30) * 1000,
    maxClassificationAttempts: positive(
      env.MAX_CLASSIFICATION_ATTEMPTS,
      "MAX_CLASSIFICATION_ATTEMPTS",
      3,
    ),
    maxActionAttempts: positive(env.MAX_ACTION_ATTEMPTS, "MAX_ACTION_ATTEMPTS", 5),
    retryBaseMs: positive(env.RETRY_BASE_SECONDS, "RETRY_BASE_SECONDS", 5) * 1000,
    recoveryIntervalMs:
      positive(env.RECOVERY_INTERVAL_SECONDS, "RECOVERY_INTERVAL_SECONDS", 300) * 1000,
    journalPath: resolve(env.JOURNAL_PATH ?? "./data/events.jsonl"),
    observer: configuredObserver
      ? {
          apiId: positive(env.OBSERVER_API_ID, "OBSERVER_API_ID", 0),
          apiHash: env.OBSERVER_API_HASH!,
          session: env.OBSERVER_SESSION!,
        }
      : undefined,
  };
}
