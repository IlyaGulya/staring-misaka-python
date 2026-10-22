import { test, expect } from "bun:test";
import { mkdtempSync, symlinkSync, linkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Database } from "bun:sqlite";
import { loadSettings } from "../src/config";
import { acquireProcessLock } from "../src/process-lock";

const base = {
  BOT_TOKEN: "123:test",
  ANTHROPIC_API_KEY: "synthetic",
  ADMIN_ID: "1",
  TRACKING_CHAT_IDS: "-1001234567890",
};

test("live mutations require both deliberate opt-in and acknowledged observation coverage", () => {
  expect(() => loadSettings({ ...base, MODE: "live" })).toThrow();
  expect(() => loadSettings({ ...base, MODE: "live", ALLOW_LIVE_MODERATION: "true" })).toThrow();
  expect(
    loadSettings({
      ...base,
      MODE: "live",
      ALLOW_LIVE_MODERATION: "true",
      ACKNOWLEDGE_OBSERVER_GAP: "true",
    }).mode,
  ).toBe("live");
  expect(() =>
    loadSettings({
      ...base,
      MODE: "live",
      ALLOW_LIVE_MODERATION: "true",
      OBSERVER_API_ID: "1",
      OBSERVER_API_HASH: "secret",
    }),
  ).toThrow();
});

test("a second executor cannot bypass the process lock using directory or database symlinks", () => {
  const dir = mkdtempSync(join(tmpdir(), "misaka-lock-"));
  try {
    const file = join(dir, "state.sqlite");
    new Database(file).close();
    symlinkSync(dir, join(dir, "alias"));
    symlinkSync(file, join(dir, "file-link.sqlite"));
    const release = acquireProcessLock(file);
    try {
      expect(() => acquireProcessLock(join(dir, "alias/state.sqlite"))).toThrow();
      expect(() => acquireProcessLock(join(dir, "file-link.sqlite"))).toThrow();
    } finally {
      release();
    }
    acquireProcessLock(join(dir, "file-link.sqlite"))();
    linkSync(file, join(dir, "hard-link.sqlite"));
    expect(() => acquireProcessLock(file)).toThrow();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("separate processes exclude a second executor and recover after SIGKILL", async () => {
  const dir = mkdtempSync(join(tmpdir(), "misaka-processes-"));
  const file = join(dir, "state.sqlite");
  const module = new URL("../src/process-lock.ts", import.meta.url).href;
  const prefix = `import { acquireProcessLock } from ${JSON.stringify(module)}; const release = acquireProcessLock(${JSON.stringify(file)});`;
  const owner = Bun.spawn(
    [process.execPath, "-e", `${prefix} console.log("LOCKED"); await Bun.stdin.text(); release();`],
    { stdin: "pipe", stdout: "pipe", stderr: "pipe" },
  );
  try {
    const reader = owner.stdout.getReader();
    try {
      const first = await reader.read();
      expect(new TextDecoder().decode(first.value)).toContain("LOCKED");
    } finally {
      reader.releaseLock();
    }
    const contender = Bun.spawn([process.execPath, "-e", `${prefix} release();`], {
      stdout: "pipe",
      stderr: "pipe",
    });
    const error = await new Response(contender.stderr).text();
    expect(await contender.exited).toBe(1);
    expect(error).toContain("Another process owns");
    owner.kill("SIGKILL");
    await owner.exited;
    const recovered = Bun.spawn([process.execPath, "-e", `${prefix} release();`], {
      stdout: "pipe",
      stderr: "pipe",
    });
    expect(await recovered.exited).toBe(0);
  } finally {
    owner.kill("SIGKILL");
    await owner.exited;
    rmSync(dir, { recursive: true, force: true });
  }
}, 10_000);
