import { lstatSync, mkdirSync, realpathSync, statSync } from "node:fs";
import { basename, dirname, isAbsolute, resolve } from "node:path";
import { loadSettings } from "./config.ts";
import { acquireProcessLock } from "./process-lock.ts";
import { Store } from "./store.ts";

class ImportUsageError extends Error {}

const USAGE =
  "Usage: bun run import --source /absolute/legacy.sqlite --target /absolute/new/misaka.sqlite";

/** Import never contacts Telegram or modifies the legacy SQLite source. */
export function importLegacyCli(
  args: readonly string[],
  env: Record<string, string | undefined> = process.env,
): Record<string, number> {
  const options: Record<string, string> = {};
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    const value = args[index + 1];
    if (
      (key !== "--source" && key !== "--target") ||
      !value ||
      value.startsWith("--") ||
      options[key]
    ) {
      throw new ImportUsageError(USAGE);
    }
    options[key] = value;
  }
  const sourceOption = options["--source"];
  const targetOption = options["--target"];
  if (!sourceOption || !targetOption || !isAbsolute(sourceOption) || !isAbsolute(targetOption)) {
    throw new ImportUsageError(
      "Both --source and --target must be explicit absolute paths. The target must be a new file.",
    );
  }
  const source = realpathSync(sourceOption);
  if (!statSync(source).isFile())
    throw new ImportUsageError("The legacy source must be a regular SQLite file.");
  const targetPath = resolve(targetOption);
  if (!/\.(?:sqlite|sqlite3|db)$/i.test(basename(targetPath))) {
    throw new ImportUsageError("The new target must have a .sqlite, .sqlite3 or .db extension.");
  }
  mkdirSync(dirname(targetPath), { recursive: true });
  const target = resolve(realpathSync(dirname(targetPath)), basename(targetPath));
  if (source === target)
    throw new ImportUsageError("The source and target must be different files.");

  const release = acquireProcessLock(target);
  let store: Store | undefined;
  try {
    // Never migrate, overwrite or merge an existing database. lstat also rejects dangling symlinks.
    try {
      lstatSync(target);
      throw new ImportUsageError(
        "The target already exists. Choose a new target; existing databases are never modified by this CLI.",
      );
    } catch (error) {
      const missingTarget = error instanceof Error && "code" in error && error.code === "ENOENT";
      if (!missingTarget) throw error;
    }
    const settings = loadSettings({ ...env, MODE: "shadow", DB_PATH: target }, false);
    store = new Store(settings);
    return store.importLegacy(source);
  } finally {
    store?.close();
    release();
  }
}

if (import.meta.main) {
  if (process.argv.includes("--help")) {
    console.log(
      `${USAGE}\nReads the legacy source without writes; creates a new shadow database. Never contacts Telegram.`,
    );
  } else {
    try {
      const counts = importLegacyCli(process.argv.slice(2));
      console.log(JSON.stringify({ imported: counts }));
    } catch (error) {
      // Do not print raw SQLite/config/SDK errors or environment secrets.
      const message =
        error instanceof ImportUsageError
          ? error.message
          : "Legacy import failed. Check the read-only source, spam configuration, target directory permissions and process lock. A failed target must be inspected or replaced with a fresh target before retrying.";
      console.error(message);
      process.exitCode = 1;
    }
  }
}
