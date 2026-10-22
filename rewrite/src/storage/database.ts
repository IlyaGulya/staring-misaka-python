import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

/** Version 1 is unchanged: reopening an existing rewrite database requires no migration. */
export function openDatabase(path: string): Database {
  if (path !== ":memory:") {
    mkdirSync(dirname(path), { recursive: true });
  }
  const database = new Database(path, { create: true, strict: true });
  database.exec(`
  PRAGMA journal_mode=WAL;
  PRAGMA synchronous=FULL;
  PRAGMA foreign_keys=ON;
  PRAGMA busy_timeout=5000;
  CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS chats (
    chatId INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    nextNotify INTEGER NOT NULL DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS chat_aliases (
    aliasId INTEGER PRIMARY KEY,
    canonicalId INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS chat_routes (
    canonicalId INTEGER PRIMARY KEY,
    destinationId INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS subjects (
    chatId INTEGER NOT NULL,
    userId INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    approved TEXT,
    processedSeq INTEGER NOT NULL DEFAULT 0,
    activeDecision TEXT,
    restrictionEpoch INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chatId,userId)
  );
  CREATE TABLE IF NOT EXISTS observations (
    source TEXT NOT NULL,
    sourceKey TEXT NOT NULL,
    payload TEXT NOT NULL,
    createdAt INTEGER NOT NULL,
    PRIMARY KEY(source,sourceKey)
  );
  CREATE TABLE IF NOT EXISTS messages (
    chatId INTEGER NOT NULL,
    messageId INTEGER NOT NULL,
    userId INTEGER,
    revision INTEGER NOT NULL,
    stamp INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chatId,messageId)
  );
  CREATE TABLE IF NOT EXISTS revisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL,
    messageId INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    userId INTEGER,
    actorKind TEXT NOT NULL,
    text TEXT NOT NULL,
    media TEXT NOT NULL,
    metadata TEXT NOT NULL,
    source TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    mode TEXT NOT NULL,
    UNIQUE(chatId,messageId,revision)
  );
  CREATE INDEX IF NOT EXISTS revisions_subject ON revisions(chatId,userId,seq);
  CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    chatId INTEGER NOT NULL,
    userId INTEGER NOT NULL,
    highwater INTEGER NOT NULL,
    payload TEXT NOT NULL,
    createdAt INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    snapshotId TEXT NOT NULL REFERENCES snapshots(id),
    chatId INTEGER NOT NULL,
    userId INTEGER NOT NULL,
    state TEXT NOT NULL,
    mode TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    token TEXT NOT NULL DEFAULT '',
    retryAt INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    createdAt INTEGER NOT NULL
  );
  CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_subject ON jobs(chatId,userId)
    WHERE state IN ('pending','running','retry','blocked');
  CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    chatId INTEGER NOT NULL,
    userId INTEGER NOT NULL,
    snapshotId TEXT NOT NULL REFERENCES snapshots(id),
    payload TEXT NOT NULL,
    createdAt INTEGER NOT NULL,
    status TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS decisions_subject ON decisions(userId,createdAt);
  CREATE TABLE IF NOT EXISTS overrides (
    decisionId TEXT PRIMARY KEY REFERENCES decisions(id),
    actorId INTEGER NOT NULL,
    createdAt INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    decisionId TEXT NOT NULL REFERENCES decisions(id),
    chatId INTEGER NOT NULL,
    userId INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    messageId INTEGER,
    messageChatId INTEGER,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    token TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    mode TEXT NOT NULL,
    retryAt INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    undoRequested INTEGER NOT NULL DEFAULT 0,
    owned INTEGER NOT NULL DEFAULT 0,
    ownershipEpoch INTEGER,
    preflightEpoch INTEGER,
    sentAt INTEGER,
    confirmedAt INTEGER,
    createdAt INTEGER NOT NULL,
    UNIQUE(kind,decisionId,messageChatId,messageId)
  );
  CREATE UNIQUE INDEX IF NOT EXISTS actions_one_ban ON actions(decisionId,kind)
    WHERE kind IN ('ban','unban','notify');
  CREATE INDEX IF NOT EXISTS actions_ready ON actions(state,retryAt);
  CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workType TEXT NOT NULL,
    workId TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    token TEXT NOT NULL,
    startedAt INTEGER NOT NULL,
    finishedAt INTEGER,
    outcome TEXT,
    error TEXT
  );
  CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    retryAt INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    createdAt INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    createdAt INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS legacy_records (
    tableName TEXT NOT NULL,
    legacyId TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(tableName,legacyId)
  );
  PRAGMA user_version=1;
  
`);
  return database;
}
