import type { StorageContext } from "./context";

/** Cancel leases and consume their evidence inside the caller's approval/migration transaction. */
export function invalidateJobs(context: StorageContext, chatId: number, userId: number): void {
  const jobs = context.all<{ id: string; token: string; highwater: number }>(
    `
    SELECT j.id, j.token, s.highwater
    FROM jobs j
    JOIN snapshots s ON s.id = j.snapshotId
    WHERE j.chatId = ? AND j.userId = ?
      AND j.state IN ('pending', 'running', 'retry', 'blocked')
  `,
    chatId,
    userId,
  );

  for (const job of jobs) {
    context.run(
      `
      UPDATE subjects
      SET processedSeq = MAX(processedSeq, ?)
      WHERE chatId = ? AND userId = ?
    `,
      job.highwater,
      chatId,
      userId,
    );
    context.finishAttempt("classification", job.id, job.token, "cancelled");
  }

  context.run(
    `
    UPDATE jobs
    SET state = 'cancelled', token = ''
    WHERE chatId = ? AND userId = ?
      AND state IN ('pending', 'running', 'retry', 'blocked')
  `,
    chatId,
    userId,
  );
}
