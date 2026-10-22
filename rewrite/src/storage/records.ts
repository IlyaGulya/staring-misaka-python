import type { ActionDetail, ActorKind, Mode, Observation } from "../domain";

/** Exact columns of subjects, used by version and restriction ownership fences. */
export interface SubjectRecord {
  chatId: number;
  userId: number;
  version: number;
  generation: number;
  approved: string | null;
  processedSeq: number;
  activeDecision: string | null;
  restrictionEpoch: number;
}

export interface RevisionRecord {
  seq: number;
  chatId: number;
  messageId: number;
  revision: number;
  userId: number | null;
  actorKind: ActorKind;
  text: string;
  media: string;
  metadata: string;
  source: Observation["source"];
  eligible: number;
  mode: Mode;
}

export interface MessageEvidenceRecord {
  chatId: number;
  messageId: number;
  userId: number | null;
  revision: number;
  stamp: number;
  deleted: number;
  text: string;
  media: string;
  metadata: string;
  source: Observation["source"];
  seq: number;
  eligible: number;
  mode: Mode;
}

export interface ClassificationRecord {
  id: string;
  snapshotId: string;
  chatId: number;
  userId: number;
  state: "pending" | "running" | "retry" | "blocked" | "cancelled" | "done";
  mode: Mode;
  attempt: number;
  token: string;
  retryAt: number;
  error: string | null;
  createdAt: number;
}

export interface ClassificationSnapshotRecord extends ClassificationRecord {
  payload: string;
}

export interface DecisionRecord {
  id: string;
  chatId: number;
  userId: number;
  snapshotId: string;
  payload: string;
  createdAt: number;
  status: string;
  overriddenAt: number | null;
}

export interface ActionRecord extends Omit<ActionDetail, "payload" | "attempts"> {
  payload: string;
}

export interface CommandRecord {
  id: string;
  payload: string;
  state: "pending" | "running" | "retry" | "done";
  attempt: number;
  retryAt: number;
  applied: number;
  error: string | null;
  createdAt: number;
}

export interface AuditRecord {
  seq: number;
  kind: string;
  payload: string;
  createdAt: number;
}
