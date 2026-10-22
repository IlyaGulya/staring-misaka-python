import {
  closeSync,
  existsSync,
  fsyncSync,
  ftruncateSync,
  mkdirSync,
  openSync,
  readSync,
  renameSync,
  statSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { dirname } from "node:path";
import { AppError } from "../domain";
import { StorageContext } from "./context";
import type { AuditRecord } from "./records";
import { encode, decode, ObjectSchema } from "./codecs";

/** At-least-once JSONL export: fsync records before advancing the SQLite cursor.
 * A crash may duplicate records; seq is the stable deduplication key.
 */
export function exportJournal(context: StorageContext): number {
  const cursor = Number(
    context.get<{
      value: string;
    }>(`
  SELECT value
  FROM meta
  WHERE key='journalCursor'
`)?.value ?? 0,
  );
  const rows = context.all<AuditRecord>(
    `
  SELECT *
  FROM audit
  WHERE seq>?
  ORDER BY seq
  LIMIT 1000
`,
    cursor,
  );
  if (!rows.length) {
    return 0;
  }
  mkdirSync(dirname(context.settings.journalPath), { recursive: true });
  if (existsSync(context.settings.journalPath)) {
    truncatePartialJournal(context.settings.journalPath);
  }
  if (
    existsSync(context.settings.journalPath) &&
    statSync(context.settings.journalPath).size >= 20 * 1024 * 1024
  ) {
    const oldest = `${context.settings.journalPath}.5`;
    if (existsSync(oldest)) {
      unlinkSync(oldest);
    }
    for (let index = 4; index >= 1; index--) {
      const previous = `${context.settings.journalPath}.${index}`;
      if (existsSync(previous)) {
        renameSync(previous, `${context.settings.journalPath}.${index + 1}`);
      }
    }
    renameSync(context.settings.journalPath, `${context.settings.journalPath}.1`);
  }
  const fd = openSync(context.settings.journalPath, "a", 0o600);
  try {
    const buffer = Buffer.from(
      rows
        .map((row) =>
          encode({
            seq: row.seq,
            kind: row.kind,
            createdAt: row.createdAt,
            payload: decode(row.payload, ObjectSchema),
          }),
        )
        .join("\n") + "\n",
    );
    let offset = 0;
    while (offset < buffer.length) {
      offset += writeSync(fd, buffer, offset, buffer.length - offset);
    }
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  context.run(
    `
  INSERT INTO meta(key,value)
  VALUES('journalCursor',?)
  ON CONFLICT(key)
  DO UPDATE SET value=excluded.value
`,
    String(rows[rows.length - 1]!.seq),
  );
  return rows.length;
}

/** A crash can leave an uncommitted partial JSONL record; SQLite remains authoritative. */
function truncatePartialJournal(path: string): void {
  const size = statSync(path).size;
  if (!size) {
    return;
  }
  const fd = openSync(path, "r+");
  try {
    const buffer = Buffer.alloc(Math.min(size, 4096));
    let position = size;
    while (position > 0) {
      const count = Math.min(position, buffer.length);
      position -= count;
      const read = readSync(fd, buffer, 0, count, position);
      if (read !== count) {
        throw new AppError({
          category: "transient",
          message: "Journal changed during tail recovery",
        });
      }
      const newline = buffer.subarray(0, count).lastIndexOf(10);
      if (newline >= 0) {
        const complete = position + newline + 1;
        if (complete !== size) {
          ftruncateSync(fd, complete);
          fsyncSync(fd);
        }
        return;
      }
    }
    ftruncateSync(fd, 0);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}
