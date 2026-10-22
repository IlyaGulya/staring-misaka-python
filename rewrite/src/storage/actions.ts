import { randomUUID } from "node:crypto";
import {
  AppError,
  type Action,
  type ActionJob,
  type ActionPayload,
  type ActionState,
  type Observation,
  type Membership,
  type Settings,
} from "../domain";
import { StorageContext } from "./context";
import type { ActionRecord, SubjectRecord } from "./records";
import { encode, decode, ActionPayloadSchema } from "./codecs";
import { getDecision } from "./queries";
import { invalidateJobs } from "./subject-lifecycle";
import { notificationText, cancelledNotificationText } from "../notifications";

/** Workflows here run inside the Store-owned SQLite transaction unless documented otherwise. */
export interface ActionRequest {
  kind: Action["kind"];
  decisionId: string;
  subject: SubjectRecord;
  messageId?: number;
  payload: ActionPayload;
  retryAt?: number;
  originatingMode?: Settings["mode"];
}

export function overrideDecision(
  context: StorageContext,
  decisionId: string,
  actorId: number,
): void {
  const decision = getDecision(context, decisionId);
  if (!decision) {
    throw new AppError({ category: "invalid", message: "Unknown decision" });
  }
  const canonical = context.canonicalChatId(decision.chatId);
  if (
    !context.run(
      `
  INSERT OR IGNORE INTO overrides(decisionId,actorId,createdAt)
  VALUES(?,?,?)
`,
      decisionId,
      actorId,
      context.now(),
    )
  ) {
    return;
  }
  const snapshot = context.get<{
    highwater: number;
  }>(
    `
  SELECT highwater
  FROM snapshots
  WHERE id=?
`,
    decision.snapshotId,
  )!;
  invalidateJobs(context, canonical, decision.userId);
  context.run(
    `
  UPDATE subjects
  SET version=version+1,processedSeq=MAX(processedSeq,?)
  WHERE chatId=?
  AND userId=?
`,
    snapshot.highwater,
    canonical,
    decision.userId,
  );
  context.run(
    `
  UPDATE actions
  SET state='cancelled',token=''
  WHERE decisionId=?
  AND state IN ('pending','retry','blocked','shadow')
  AND kind IN ('ban','delete','notify')
  AND COALESCE(json_extract(payload,'$.followup'),0)=0
  AND sentAt IS NULL
`,
    decisionId,
  );
  context.run(
    `
  UPDATE actions
  SET undoRequested=1
  WHERE decisionId=?
  AND kind='ban'
`,
    decisionId,
  );
  const ban = context.get<ActionRecord>(
    `
  SELECT *
  FROM actions
  WHERE decisionId=?
  AND kind='ban'
`,
    decisionId,
  );
  if (ban?.state === "confirmed") {
    queueUndo(context, ban);
  } else if (ban && ["unknown", "sending", "reconciling", "preflight"].includes(ban.state)) {
    // Preserve the lease: a late successful receipt is necessary to establish ownership.
    context.run(
      `
  UPDATE actions
  SET retryAt=MIN(retryAt,?)
  WHERE id=?
`,
      context.now(),
      ban.id,
    );
  } else if (ban?.state === "needs_attention") {
    context.audit("undo_needs_attention", {
      decisionId,
      reason: "restriction ownership is ambiguous",
    });
  } else {
    clearActive(context, decisionId);
  }
  const notification = context.get<{
    payload: string;
  }>(
    `
  SELECT payload
  FROM actions
  WHERE decisionId=?
  AND kind='notify'
  AND state='confirmed'
`,
    decisionId,
  );
  if (notification) {
    const payload = decode(notification.payload, ActionPayloadSchema);
    if (typeof payload.receipt === "number" && Number.isSafeInteger(payload.receipt)) {
      addAction(context, {
        kind: "edit",
        decisionId: decisionId,
        subject: context.subject(canonical, decision.userId),
        messageId: payload.receipt,
        payload: {
          followup: true,
          text: cancelledNotificationText,
          messageChatId: Number(payload.sentChatId ?? context.resolveChatId(canonical)),
        },
      });
    }
  }
  context.audit("decision_overridden", { decisionId, actorId });
}

export function addAction(context: StorageContext, request: ActionRequest): void {
  const { kind, decisionId, subject, messageId, payload, retryAt = 0, originatingMode } = request;
  let mode = context.settings.mode;
  if (mode !== "shadow") {
    if (originatingMode !== undefined) {
      mode = originatingMode;
    } else {
      const ban = context.get<{ mode: Settings["mode"] }>(
        `
              SELECT mode FROM actions WHERE decisionId = ? AND kind = 'ban'
            `,
        decisionId,
      );
      mode = ban?.mode ?? context.settings.mode;
    }
  }
  const state = mode === "shadow" || context.settings.mode === "shadow" ? "shadow" : "pending";
  const id = randomUUID();
  if (
    context.run(
      `
  INSERT OR IGNORE INTO actions(id,kind,decisionId,chatId,userId,generation,messageId,messageChatId,state,payload,mode,retryAt,createdAt)
  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
`,
      id,
      kind,
      decisionId,
      subject.chatId,
      subject.userId,
      subject.generation,
      messageId ?? null,
      kind === "delete" || kind === "edit" ? Number(payload.messageChatId ?? subject.chatId) : null,
      state,
      encode(payload),
      mode,
      retryAt,
      context.now(),
    )
  ) {
    context.audit("action_scheduled", { id, kind, decisionId, state, messageId });
  }
}

function actionJob(row: ActionRecord, phase: ActionJob["phase"]): ActionJob {
  return {
    id: row.id,
    kind: row.kind,
    decisionId: row.decisionId,
    chatId: row.chatId,
    userId: row.userId,
    generation: row.generation,
    messageId: row.messageId ?? undefined,
    state: row.state,
    attempt: row.attempt,
    token: row.token,
    payload: decode(row.payload, ActionPayloadSchema),
    phase,
    undoRequested: Boolean(row.undoRequested),
  };
}

/** Only an exact, timely receipt authored by our bot can resolve an uncertain notification. */
export function reconcileObservedNotification(
  context: StorageContext,
  observation: Observation,
): void {
  if (
    (observation.kind !== "message" && observation.kind !== "edit") ||
    context.botId === 0 ||
    observation.userId !== context.botId ||
    observation.messageId === undefined ||
    !observation.text
  ) {
    return;
  }
  const decisionId = observation.text.match(/start=explain_([0-9a-f-]{36})$/)?.[1];
  if (!decisionId) {
    return;
  }
  const notification = context.get<ActionRecord>(
    `
        SELECT * FROM actions
        WHERE decisionId = ? AND kind = 'notify'
          AND state IN ('sending', 'unknown', 'needs_attention', 'reconciling')
          AND sentAt IS NOT NULL
    `,
    decisionId,
  );
  if (!notification || notification.sentAt === null) {
    return;
  }
  const payload = decode(notification.payload, ActionPayloadSchema);
  const earliestReceipt = Math.floor(notification.sentAt / 1000) * 1000;
  if (
    payload.text !== observation.text ||
    payload.sentChatId !== observation.chatId ||
    observation.occurredAt < earliestReceipt
  ) {
    return;
  }

  context.finishAttempt("action", notification.id, notification.token, "observed_confirmed");
  const token = randomUUID();
  context.run(
    `
        UPDATE actions SET state = 'sending', token = ? WHERE id = ?
    `,
    token,
    notification.id,
  );
  const recovered = actionJob({ ...notification, token }, "reconcile");
  completeAction(
    context,
    {
      ...recovered,
      payload: { ...recovered.payload, followupCreatedAt: observation.occurredAt },
    },
    observation.messageId,
  );
  context.audit("notification_receipt_observed", {
    actionId: notification.id,
    chatId: observation.chatId,
    messageId: observation.messageId,
  });
}

export function claimAction(context: StorageContext): ActionJob | undefined {
  if (context.settings.mode !== "live") {
    return undefined;
  }
  const row = context.get<ActionRecord>(
    `
        SELECT a.*
        FROM actions a
        LEFT JOIN chats c ON c.chatId = a.chatId
        WHERE a.mode = 'live'
          AND a.state IN ('pending', 'retry', 'blocked', 'unknown')
          AND a.retryAt <= ?
          AND (a.kind != 'notify' OR COALESCE(c.nextNotify, 0) <= ?)
          AND NOT EXISTS (
            SELECT 1 FROM actions busy
            WHERE busy.chatId = a.chatId AND busy.userId = a.userId
              AND busy.state IN ('preflight', 'sending', 'reconciling')
          )
        ORDER BY
          CASE a.kind
            WHEN 'unban' THEN 0
            WHEN 'ban' THEN 1
            WHEN 'edit' THEN 2
            WHEN 'delete' THEN 3
            ELSE 4
          END,
          a.retryAt, a.createdAt
        LIMIT 1
    `,
    context.now(),
    context.now(),
  );
  if (!row) {
    return undefined;
  }
  const phase = row.state === "unknown" ? "reconcile" : "preflight";
  const token = randomUUID();
  context.run(
    `
  UPDATE actions
  SET state=?,attempt=attempt+1,token=?,preflightEpoch=?
  WHERE id=?
`,
    phase === "reconcile" ? "reconciling" : "preflight",
    token,
    context.subject(row.chatId, row.userId).restrictionEpoch,
    row.id,
  );
  if (row.kind === "notify") {
    context.run(
      `
  UPDATE chats
  SET nextNotify=?
  WHERE chatId=?
`,
      context.now() + 30000,
      row.chatId,
    );
  }
  context.startAttempt("action", row.id, row.attempt + 1, token);
  return actionJob({ ...row, token, attempt: row.attempt + 1 }, phase);
}

export function prepareAction(
  context: StorageContext,
  action: ActionJob,
  membership?: Membership,
  targetChatId?: number,
): boolean {
  const row = context.get<ActionRecord>(
    `
  SELECT *
  FROM actions
  WHERE id=?
  AND token=?
  AND state IN ('preflight','reconciling')
`,
    action.id,
    action.token,
  );
  if (!row) {
    return false;
  }
  const subject = context.subject(row.chatId, row.userId);
  if (targetChatId !== undefined && targetChatId !== context.resolveChatId(row.chatId)) {
    if (action.phase === "reconcile") {
      context.run(
        `
  UPDATE actions
  SET state='unknown',token='',retryAt=?,error='destination changed during reconciliation'
  WHERE id=?
`,
        context.now() + context.settings.recoveryIntervalMs,
        row.id,
      );
      context.finishAttempt("action", row.id, row.token, "destination_changed");
      context.audit("action_reconciliation_deferred", { id: row.id });
    } else {
      retryReconciled(context, row);
    }
    return false;
  }
  if (
    (row.kind === "ban" || row.kind === "unban") &&
    row.preflightEpoch !== subject.restrictionEpoch
  ) {
    attention(context, row, "Restriction changed while membership was being checked");
    return false;
  }
  if (row.kind === "ban") {
    if (!membership) {
      throw new AppError({ category: "invalid", message: "Ban requires membership preflight" });
    }
    if (
      row.undoRequested ||
      context.get<{ 1: number }>(
        `
          SELECT 1
          FROM overrides
          WHERE decisionId=?
        `,
        row.decisionId,
      ) ||
      subject.approved ||
      subject.activeDecision !== row.decisionId
    ) {
      if (action.phase === "reconcile" && membership.restricted) {
        attention(context, row, "Interrupted ban is present but ownership cannot be proven");
      } else {
        terminalAction(context, row, "cancelled");
        clearActive(context, row.decisionId);
      }
      return false;
    }
    if (membership.restricted) {
      attention(context, row, "Existing restriction has no confirmed receipt owned by this action");
      return false;
    }
    if (action.phase === "reconcile") {
      retryReconciled(context, row);
      return false;
    }
  }
  if (row.kind === "unban") {
    if (!membership) {
      throw new AppError({ category: "invalid", message: "Unban requires membership preflight" });
    }
    if (!membership.restricted) {
      terminalAction(context, row, "confirmed");
      clearActive(context, row.decisionId);
      return false;
    }
    const ban = context.get<ActionRecord>(
      `
  SELECT *
  FROM actions
  WHERE decisionId=?
  AND kind='ban'
`,
      row.decisionId,
    );
    if (
      action.phase === "reconcile" ||
      !ban?.owned ||
      ban.ownershipEpoch !== subject.restrictionEpoch ||
      subject.activeDecision !== row.decisionId ||
      !context.get<{
        1: number;
      }>(
        `
  SELECT 1
  FROM overrides
  WHERE decisionId=?
`,
        row.decisionId,
      )
    ) {
      attention(context, row, "Cannot safely remove an ambiguous or newer restriction");
      return false;
    }
  }
  if (
    (row.kind === "delete" || row.kind === "notify") &&
    !decode(row.payload, ActionPayloadSchema).followup &&
    context.get<{ 1: number }>(
      `
      SELECT 1
      FROM overrides
      WHERE decisionId=?
    `,
      row.decisionId,
    )
  ) {
    terminalAction(context, row, "cancelled");
    return false;
  }
  if (row.kind === "notify" && action.phase === "reconcile") {
    attention(context, row, "Notification outcome unknown; not duplicated");
    return false;
  }
  if (
    row.kind === "edit" &&
    context.get<{ 1: number }>(
      `
      SELECT 1
      FROM actions
      WHERE decisionId=?
      AND kind='delete'
      AND messageId=?
      AND messageChatId=?
      AND state='confirmed'
      AND json_extract(payload,'$.followup')=1
    `,
      row.decisionId,
      row.messageId,
      row.messageChatId,
    )
  ) {
    terminalAction(context, row, "cancelled");
    return false;
  }
  if (row.kind === "notify") {
    context.run(
      `
  UPDATE actions
  SET payload=?
  WHERE id=?
`,
      encode({
        ...decode(row.payload, ActionPayloadSchema),
        text: notificationText(context.botUsername, row.decisionId),
        sentChatId: targetChatId ?? context.resolveChatId(row.chatId),
      }),
      row.id,
    );
  }
  context.run(
    `
  UPDATE actions
  SET state='sending',preflightEpoch=?,sentAt=?
  WHERE id=?
  AND token=?
`,
    subject.restrictionEpoch,
    context.now(),
    row.id,
    action.token,
  );
  return true;
}

function retryReconciled(context: StorageContext, row: ActionRecord): void {
  context.run(
    `
  UPDATE actions
  SET state='retry',token='',sentAt=NULL,retryAt=?,error='reconciled: effect absent'
  WHERE id=?
`,
    context.now() + context.delay(row.attempt, context.settings.maxActionAttempts),
    row.id,
  );
  context.finishAttempt("action", row.id, row.token, "reconciled_absent");
  context.audit("action_reconciled_absent", { id: row.id });
}

export function completeAction(context: StorageContext, action: ActionJob, receipt?: number): void {
  const row = context.get<ActionRecord>(
    `
  SELECT *
  FROM actions
  WHERE id=?
  AND token=?
  AND state='sending'
`,
    action.id,
    action.token,
  );
  if (!row) {
    return;
  }
  const subject = context.subject(row.chatId, row.userId);
  if (row.kind === "notify" && (!Number.isSafeInteger(receipt) || receipt! <= 0)) {
    throw new AppError({
      category: "unknown",
      message: "Notification has no valid message receipt",
    });
  }
  terminalAction(context, row, "confirmed");
  if (row.kind === "ban") {
    const owned = subject.restrictionEpoch === row.preflightEpoch;
    context.run(
      `
  UPDATE actions
  SET owned=?,ownershipEpoch=?
  WHERE id=?
`,
      Number(owned),
      row.preflightEpoch,
      row.id,
    );
    if (!owned) {
      attention(
        context,
        { ...row, token: "" },
        "Restriction changed concurrently with successful ban",
      );
    } else if (
      row.undoRequested ||
      context.get<{ 1: number }>(
        `
          SELECT 1
          FROM overrides
          WHERE decisionId=?
        `,
        row.decisionId,
      )
    ) {
      queueUndo(context, { ...row, owned: 1, ownershipEpoch: row.preflightEpoch });
    }
  } else if (row.kind === "unban") {
    clearActive(context, row.decisionId);
  } else if (row.kind === "delete" && row.messageId !== null) {
    context.run(
      `
  UPDATE messages
  SET deleted=1
  WHERE chatId=?
  AND messageId=?
`,
      row.messageChatId ?? row.chatId,
      row.messageId,
    );
  } else if (row.kind === "notify") {
    const sentChatId = Number(action.payload.sentChatId ?? context.resolveChatId(row.chatId));
    context.run(
      `
  UPDATE actions
  SET payload=?
  WHERE id=?
`,
      encode({ ...decode(row.payload, ActionPayloadSchema), receipt, sentChatId }),
      row.id,
    );
    addAction(context, {
      kind: "delete",
      decisionId: row.decisionId,
      subject: subject,
      messageId: receipt,
      payload: { followup: true, messageChatId: sentChatId },
      retryAt:
        Number(action.payload.followupCreatedAt ?? context.now()) + context.settings.followupTtlMs,
    });
    if (
      context.get<{ 1: number }>(
        `
          SELECT 1
          FROM overrides
          WHERE decisionId=?
        `,
        row.decisionId,
      )
    ) {
      addAction(context, {
        kind: "edit",
        decisionId: row.decisionId,
        subject: subject,
        messageId: receipt,
        payload: { followup: true, text: cancelledNotificationText, messageChatId: sentChatId },
      });
    }
  }
}

function queueUndo(context: StorageContext, ban: ActionRecord): void {
  const subject = context.subject(ban.chatId, ban.userId);
  if (
    !ban.owned ||
    subject.restrictionEpoch !== ban.ownershipEpoch ||
    subject.activeDecision !== ban.decisionId
  ) {
    attention(context, ban, "Undo refused: restriction ownership is ambiguous or superseded");
    return;
  }
  addAction(context, {
    kind: "unban",
    decisionId: ban.decisionId,
    subject: subject,
    payload: { banActionId: ban.id },
  });
}

function clearActive(context: StorageContext, decisionId: string): void {
  context.run(
    `
        UPDATE subjects
        SET activeDecision = NULL, generation = generation + 1
        WHERE activeDecision = ?
    `,
    decisionId,
  );
}

function terminalAction(
  context: StorageContext,
  row: ActionRecord,
  state: "confirmed" | "cancelled",
): void {
  context.run(
    `
  UPDATE actions
  SET state=?,token='',confirmedAt=?,error=NULL
  WHERE id=?
`,
    state,
    state === "confirmed" ? context.now() : null,
    row.id,
  );
  context.finishAttempt("action", row.id, row.token, state);
  context.audit(`action_${state}`, { id: row.id, kind: row.kind, decisionId: row.decisionId });
}

function attention(context: StorageContext, row: ActionRecord, error: string): void {
  context.run(
    `
  UPDATE actions
  SET state='needs_attention',token='',error=?
  WHERE id=?
`,
    error,
    row.id,
  );
  context.finishAttempt("action", row.id, row.token, "needs_attention", error);
  context.audit("action_needs_attention", { id: row.id, error });
}

export function failAction(context: StorageContext, action: ActionJob, error: AppError): void {
  const row = context.get<ActionRecord>(
    `
  SELECT *
  FROM actions
  WHERE id=?
  AND token=?
  AND state IN ('preflight','sending','reconciling')
`,
    action.id,
    action.token,
  );
  if (!row) {
    return;
  }
  const uncertainSend =
    row.state === "sending" && error.category !== "permanent" && error.category !== "invalid";
  let state: ActionState =
    error.category === "permanent" || error.category === "invalid" ? "blocked" : "retry";
  if (
    (row.kind === "ban" || row.kind === "unban") &&
    (uncertainSend || action.phase === "reconcile")
  ) {
    state = "unknown";
  }
  if (row.kind === "notify" && (uncertainSend || action.phase === "reconcile")) {
    state = "needs_attention";
  }
  const delay =
    state === "blocked"
      ? context.settings.recoveryIntervalMs
      : context.delay(row.attempt, context.settings.maxActionAttempts);
  context.run(
    `
  UPDATE actions
  SET state=?,token='',error=?,retryAt=?
  WHERE id=?
`,
    state,
    error.message,
    context.now() + Math.max(delay, error.retryAfterMs ?? 0),
    row.id,
  );
  context.finishAttempt("action", row.id, action.token, state, error.message);
  context.audit("action_failed", { id: row.id, attempt: row.attempt, state, error: error.message });
}
