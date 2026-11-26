import { readdirSync, statSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import type { StorageContext } from "./context.ts";

export interface RetentionReport {
  readonly prunedObservations: number;
  readonly prunedAudit: number;
  readonly prunedMedia: number;
}

/** Keep durable moderation history forever while bounding raw observations, exported audit and unused media blobs. */
export function runRetention(context: StorageContext): RetentionReport {
  const days = context.settings.retentionDays ?? 30;
  if (days <= 0) {
    return { prunedObservations: 0, prunedAudit: 0, prunedMedia: 0 };
  }
  const cutoff = context.now() - days * 86_400_000;

  const prunedObservations = context.run(
    `
    DELETE FROM observations
    WHERE createdAt < ?
  `,
    cutoff,
  );

  // Never delete audit rows that have not been exported to JSONL yet.
  const journalCursor = Number(
    context.get<{ value: string }>(`SELECT value FROM meta WHERE key = 'journalCursor'`)?.value ??
      0,
  );
  const prunedAudit = context.run(
    `
    DELETE FROM audit
    WHERE createdAt < ? AND seq <= ?
  `,
    cutoff,
    journalCursor,
  );

  let prunedMedia = 0;
  if (context.settings.dbPath !== ":memory:") {
    const mediaDir = join(dirname(context.settings.dbPath), "media");
    let entries: string[] = [];
    try {
      entries = readdirSync(mediaDir);
    } catch {
      entries = [];
    }
    if (entries.length > 0) {
      // Protect media blobs referenced by any spam decision or active job snapshot.
      const protectedHashes = new Set<string>();
      const snapshotPayloads = context.all<{ payload: string }>(`
        SELECT s.payload
        FROM snapshots s
        WHERE EXISTS (
          SELECT 1 FROM decisions d
          WHERE d.snapshotId = s.id AND json_extract(d.payload, '$.verdict') = 'spam'
        )
        OR EXISTS (
          SELECT 1 FROM jobs j
          WHERE j.snapshotId = s.id AND j.state IN ('pending', 'running', 'retry', 'blocked')
        )
      `);
      for (const row of snapshotPayloads) {
        const matches = row.payload.match(/[a-f0-9]{64}/g);
        if (matches) {
          for (const hash of matches) protectedHashes.add(hash);
        }
      }
      for (const name of entries) {
        if (!/^[a-f0-9]{64}$/.test(name) || protectedHashes.has(name)) continue;
        const fullPath = join(mediaDir, name);
        try {
          const info = statSync(fullPath);
          if (info.isFile() && info.mtimeMs < cutoff) {
            unlinkSync(fullPath);
            prunedMedia++;
          }
        } catch {
          // Ignore races with concurrent cleanup or missing files.
        }
      }
    }
  }

  if (prunedObservations > 0 || prunedAudit > 0 || prunedMedia > 0) {
    context.audit("retention_pruned", {
      retentionDays: days,
      prunedObservations,
      prunedAudit,
      prunedMedia,
    });
  }
  try {
    context.db.exec("PRAGMA wal_checkpoint(PASSIVE); PRAGMA optimize;");
  } catch {
    // Non-fatal maintenance hint.
  }
  return { prunedObservations, prunedAudit, prunedMedia };
}
