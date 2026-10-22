import { Database } from "bun:sqlite";
import { existsSync, mkdirSync, realpathSync, statSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";

/** An OS-released SQLite lock on a separate file; never holds the application DB transaction. */
export function acquireProcessLock(dbPath: string): () => void {
  if (dbPath === ":memory:") return () => {};
  const requested = resolve(dbPath);
  mkdirSync(dirname(requested), { recursive: true });
  const path = existsSync(requested)
    ? realpathSync(requested)
    : join(realpathSync(dirname(requested)), basename(requested));
  if (existsSync(path) && statSync(path).nlink > 1)
    throw new Error("Hard-linked database paths are not supported");
  const lock = new Database(`${path}.process-lock`, { create: true });
  try {
    // SQLite must have a materialized database page before a cross-process lock is held.
    lock.exec(
      "PRAGMA busy_timeout=0; CREATE TABLE IF NOT EXISTS executor_lock (id INTEGER PRIMARY KEY); BEGIN EXCLUSIVE",
    );
  } catch {
    lock.close();
    throw new Error(
      "Another process owns this database; stop its receiver/executor before starting or importing",
    );
  }
  return () => {
    lock.close();
  };
}
