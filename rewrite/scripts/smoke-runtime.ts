import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { Database } from "bun:sqlite";
import { Schema } from "effect";
import type { Generated } from "@effect/ai-anthropic";
import type { Update } from "grammy/types";

// Executed inside the production image by smoke-image.sh, with --network none.
assert.notEqual(process.getuid?.(), 0, "Production image must run as non-root");
assert.equal(process.platform, "linux");
let phase = "smoke setup";
const deadline = setTimeout(() => {
  console.error(JSON.stringify({ result: "TIMEOUT", phase, modelRequests, polls }));
  process.exit(1);
}, 60_000);
deadline.unref();

const chatId = -1000000000123;
const userId = 42;
const botId = 777;
const text = "private-container-smoke-message";
const key = "synthetic-container-api-key";
const token = "777:synthetic-container-token";
const path = "/app/data/smoke.sqlite";
const journal = "/app/data/events.jsonl";
const updates: Update[] = [];
const methods = new Set<string>();
const violations: string[] = [];
let modelRequests = 0;
let polls = 0;

const Poll = Schema.Struct({ offset: Schema.optional(Schema.Int) });
const NativeRequest = Schema.Struct({
  output_config: Schema.Struct({ format: Schema.Struct({ type: Schema.Literal("json_schema") }) }),
  tools: Schema.optional(Schema.Array(Schema.Unknown)),
});
const Log = Schema.Struct({ event: Schema.String });
const Audit = Schema.Struct({ seq: Schema.Int, kind: Schema.String });

const server = Bun.serve({
  hostname: "127.0.0.1",
  port: 0,
  async fetch(request) {
    try {
      const url = new URL(request.url);
      if (url.pathname === "/v1/messages") {
        const body = Schema.decodeUnknownSync(NativeRequest)(await request.json());
        assert.equal(body.tools?.length ?? 0, 0, "Classifier must not expose tools");
        assert.equal(request.headers.get("x-api-key"), key);
        modelRequests++;
        assert(modelRequests <= 2, "Restart must not repeat completed classifications");
        return Response.json({
          id: `msg_${modelRequests}`,
          type: "message",
          role: "assistant",
          model: "claude-sonnet-4-5-20250929",
          stop_reason: "end_turn",
          stop_sequence: null,
          content: [
            {
              type: "text",
              text: JSON.stringify({
                verdict: modelRequests === 1 ? "clean" : "spam",
                reason: "Synthetic container smoke finding",
                evidenceMessageIds: [10],
              }),
            },
          ],
          usage: {
            input_tokens: 12,
            output_tokens: 8,
            cache_creation_input_tokens: 0,
            cache_creation: null,
            service_tier: "standard",
            cache_read_input_tokens: 0,
          },
        } satisfies Generated.BetaMessage);
      }
      const method = url.pathname.replace(`/bot${token}/`, "");
      methods.add(method);
      const ok = (result: unknown) => Response.json({ ok: true, result });
      switch (method) {
        case "getMe":
          return ok({ id: botId, is_bot: true, first_name: "Smoke", username: "smoke_bot" });
        case "getWebhookInfo":
          return ok({ url: "", pending_update_count: 0 });
        case "getChatMember":
          return ok({
            user: { id: botId, is_bot: true, first_name: "Smoke" },
            status: "administrator",
            can_restrict_members: true,
          });
        case "getUpdates": {
          const body = Schema.decodeUnknownSync(Poll)(await request.json());
          polls++;
          await Bun.sleep(50);
          return ok(updates.filter((update) => update.update_id >= (body.offset ?? 0)));
        }
        default:
          throw new Error(`Unexpected Telegram operation: ${method}`);
      }
    } catch (error) {
      violations.push(error instanceof Error ? error.message : String(error));
      return Response.json(
        { ok: false, error_code: 500, description: "Smoke fixture rejected request" },
        { status: 500 },
      );
    }
  },
});

await Bun.write(
  "/tmp/policy.yaml",
  'system_prompt: "Classify synthetic smoke evidence"\nmodel: claude-sonnet-4-5-20250929\n',
);
const env = {
  ...process.env,
  MODE: "shadow",
  ALLOW_LIVE_MODERATION: "false",
  BOT_TOKEN: token,
  ANTHROPIC_API_KEY: key,
  ADMIN_ID: "1",
  TRACKING_CHAT_IDS: String(chatId),
  SPAM_CONFIG_PATH: "/tmp/policy.yaml",
  DB_PATH: path,
  JOURNAL_PATH: journal,
  ANTHROPIC_BASE_URL: server.url.origin,
  TELEGRAM_API_ROOT: server.url.origin,
  OBSERVER_API_ID: "",
  OBSERVER_API_HASH: "",
  OBSERVER_SESSION: "",
  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "",
  POLL_TIMEOUT: "1",
  CLASSIFICATION_CONCURRENCY: "1",
  CLASSIFICATION_TIMEOUT_SECONDS: "5",
};

function launch(...args: string[]) {
  const child = Bun.spawn([process.execPath, "src/main.ts", ...args], {
    cwd: "/app",
    env,
    stdout: "pipe",
    stderr: "pipe",
  });
  const output = Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ]);
  return { child, output };
}
let running: ReturnType<typeof launch> | undefined;
let database: Database | undefined;
const logs: string[] = [];

async function until(check: () => boolean, label: string) {
  phase = label;
  const end = Date.now() + 15_000;
  while (!check()) {
    assert.equal(violations.length, 0, violations.join("\n"));
    if (running && running.child.exitCode !== null) {
      throw new Error(
        `CLI exited while waiting for ${label}: ${(await running.output).join("\n")}`,
      );
    }
    if (Date.now() >= end) throw new Error(`Timed out waiting for ${label}`);
    await Bun.sleep(25);
  }
}

async function stop() {
  phase = "graceful shutdown";
  assert(running);
  running.child.kill("SIGTERM");
  const timeout = setTimeout(() => running?.child.kill("SIGKILL"), 5_000);
  try {
    const code = await running.child.exited;
    const output = (await running.output).join("\n");
    logs.push(output);
    assert([0, 130].includes(code), `Unclean CLI exit ${code}: ${output}`);
    const events = output
      .split("\n")
      .filter((line) => line.startsWith("{"))
      .map((line) => Schema.decodeUnknownSync(Log)(JSON.parse(line)));
    assert(
      events.some((entry) => entry.event === "stopped"),
      "SIGTERM must close resources",
    );
  } finally {
    clearTimeout(timeout);
    running = undefined;
  }
}

function verdicts() {
  return database!
    .query<{ verdict: string }, []>(
      "SELECT json_extract(payload,'$.verdict') AS verdict FROM decisions ORDER BY rowid",
    )
    .all()
    .map((row) => row.verdict);
}

try {
  phase = "configuration check";
  running = launch("--check-config");
  assert.equal(await running.child.exited, 0, (await running.output).join("\n"));
  running = undefined;
  assert(!existsSync(path), "Configuration check must not create a database");
  assert.equal(methods.size + modelRequests, 0, "Configuration check must not use the network");

  running = launch();
  await until(() => existsSync(path) && polls > 0, "receiver startup");
  database = new Database(path, { readonly: true, strict: true });
  const message = {
    message_id: 10,
    date: Math.floor(Date.now() / 1000),
    chat: { id: chatId, type: "supergroup" as const, title: "Smoke" },
    from: { id: userId, is_bot: false, first_name: "Sender" },
    text,
  };
  updates.push({ update_id: 1, message });
  await until(() => verdicts().length === 1, "clean decision");
  assert.deepEqual(verdicts(), ["clean"]);
  assert.equal(
    database
      .query<{ approved: string | null }, []>("SELECT approved FROM subjects WHERE userId=42")
      .get()!.approved,
    null,
    "Clean must not grant permanent trust",
  );

  updates.push({
    update_id: 2,
    edited_message: { ...message, edit_date: message.date + 1, text: `${text}-edited` },
  });
  await until(() => verdicts().length === 2, "edited spam decision");
  assert.deepEqual(verdicts(), ["clean", "spam"]);
  await stop();

  const pollsBeforeRestart = polls;
  running = launch();
  await until(() => polls >= pollsBeforeRestart + 3, "receiver restart");
  await stop();
  assert.equal(modelRequests, 2, "Restart must preserve update offset and completed jobs");
  assert.deepEqual(verdicts(), ["clean", "spam"]);
  const actions = database
    .query<{ kind: string; state: string; mode: string; attempt: number }, []>(
      "SELECT kind,state,mode,attempt FROM actions",
    )
    .all();
  assert.deepEqual(actions.map((action) => action.kind).sort(), ["ban", "delete", "notify"]);
  assert(
    actions.every(
      (action) => action.mode === "shadow" && action.state === "shadow" && action.attempt === 0,
    ),
  );
  assert.equal(
    database.query<{ integrity_check: string }, []>("PRAGMA integrity_check").get()!
      .integrity_check,
    "ok",
  );
  const audit = (await Bun.file(journal).text())
    .trim()
    .split("\n")
    .map((line) => Schema.decodeUnknownSync(Audit)(JSON.parse(line)));
  const decisionSeqs = database
    .query<{ seq: number }, []>("SELECT seq FROM audit WHERE kind='decision'")
    .all();
  assert.equal(decisionSeqs.length, 2);
  assert(
    decisionSeqs.every((row) =>
      audit.some((entry) => entry.seq === row.seq && entry.kind === "decision"),
    ),
    "JSONL must contain persisted decisions",
  );
  assert.equal(violations.length, 0, violations.join("\n"));
  for (const secret of [text, key, token])
    assert(!logs.join("\n").includes(secret), "Runtime logs leaked private data");
  console.log(
    JSON.stringify({
      result: "PASS",
      platform: `${process.platform}/${process.arch}`,
      uid: process.getuid?.(),
      verdicts: verdicts(),
      nativeRequests: modelRequests,
      restart: "PASS",
      shadowPlans: actions.length,
      executedActions: 0,
      journal: "PASS",
      telegramMethods: [...methods],
    }),
  );
} catch (error) {
  console.error(
    JSON.stringify({
      result: "FAIL",
      phase,
      modelRequests,
      polls,
      telegramMethods: [...methods],
      violations,
      jobs: database
        ?.query<{ state: string; attempt: number; error: string | null }, []>(
          "SELECT state,attempt,error FROM jobs",
        )
        .all(),
    }),
  );
  throw error;
} finally {
  if (running) {
    running.child.kill("SIGKILL");
    await running.child.exited;
    console.error((await running.output).join("\n"));
  }
  database?.close();
  server.stop(true);
  clearTimeout(deadline);
}
