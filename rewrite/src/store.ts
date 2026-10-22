import {
  AppError,
  type ActionDetail,
  type ActionJob,
  type Classification,
  type ClassificationJob,
  type Command,
  type Decision,
  type Membership,
  type Observation,
  type Settings,
  type StoreHealth,
} from "./domain";
import { StorageContext } from "./storage/context";
import { recoverInterruptedWork } from "./storage/recovery";
import { ingestObservation } from "./storage/ingestion";
import * as classification from "./storage/classification";
import * as actions from "./storage/actions";
import * as administration from "./storage/administration";
import * as queries from "./storage/queries";
import { exportJournal } from "./storage/journal";
import { importLegacy } from "./storage/legacy";

/** Application entrypoint: synchronous immediate transactions own every state transition.
 * Workflow modules share this connection, but cannot independently commit a partial batch.
 */
export class Store {
  private readonly context: StorageContext;

  constructor(
    readonly settings: Settings,
    now: () => number = Date.now,
  ) {
    this.context = new StorageContext(settings, now);
    this.context.atomic(() => recoverInterruptedWork(this.context));
  }

  close(): void {
    this.context.db.close();
  }

  bindBot(botId: number, botUsername?: string): void {
    const context = this.context;
    context.atomic(() => {
      const existing = context.get<{ value: string }>(`
        SELECT value FROM meta WHERE key = 'botId'
      `)?.value;
      if (existing !== undefined && Number(existing) !== botId) {
        throw new AppError({
          category: "invalid",
          message: "Database belongs to another Telegram bot",
        });
      }
      context.run(
        `
        INSERT OR IGNORE INTO meta(key, value) VALUES('botId', ?)
      `,
        String(botId),
      );
      context.botId = botId;
      if (botUsername !== undefined) {
        context.run(
          `
          INSERT INTO meta(key, value) VALUES('botUsername', ?)
          ON CONFLICT(key) DO UPDATE SET value = excluded.value
        `,
          botUsername,
        );
      }
      context.botUsername =
        botUsername ??
        context.get<{ value: string }>(`
        SELECT value FROM meta WHERE key = 'botUsername'
      `)?.value ??
        "";
    });
  }

  canonicalChatId(chatId: number): number {
    return this.context.canonicalChatId(chatId);
  }

  resolveChatId(chatId: number): number {
    return this.context.resolveChatId(chatId);
  }

  tracksChat(chatId: number): boolean {
    return this.context.tracksChat(chatId);
  }

  ingest(observation: Observation): boolean {
    return this.context.atomic(() => ingestObservation(this.context, observation));
  }

  getOffset(): number {
    return queries.getOffset(this.context);
  }

  /** Evidence, durable commands and accepted poll offset are one indivisible commit. */
  acceptBatch(observations: Observation[], nextOffset: number, commands: Command[] = []): void {
    const context = this.context;
    context.atomic(() => {
      for (const observation of observations) {
        ingestObservation(context, observation);
      }
      for (const command of commands) {
        administration.enqueueCommand(context, command);
      }
      context.run(
        `
        INSERT INTO meta(key, value) VALUES('offset', ?)
        ON CONFLICT(key) DO UPDATE SET value = CAST(
          MAX(CAST(meta.value AS INTEGER), CAST(excluded.value AS INTEGER)) AS TEXT
        )
      `,
        String(nextOffset),
      );
    });
  }

  enqueueCommand(command: Command): void {
    this.context.atomic(() => administration.enqueueCommand(this.context, command));
  }

  claimCommand(): Command | undefined {
    return this.context.atomic(() => administration.claimCommand(this.context));
  }

  applyCommand<T>(id: string, mutation: () => T): T | undefined {
    return this.context.atomic(() => administration.applyCommand(this.context, id, mutation));
  }

  finishCommand(id: string): void {
    administration.finishCommand(this.context, id);
  }

  failCommand(id: string, error: string): void {
    this.context.atomic(() => administration.failCommand(this.context, id, error));
  }

  getDecision(id: string): Decision | undefined {
    return queries.getDecision(this.context, id);
  }

  decisionsFor(userId: number): Decision[] {
    return queries.decisionsFor(this.context, userId);
  }

  decisionForMessage(chatId: number, messageId: number): Decision | undefined {
    return queries.decisionForMessage(this.context, chatId, messageId);
  }

  approve(chatId: number, userId: number, actorId: number): void {
    this.context.atomic(() => administration.approve(this.context, chatId, userId, actorId));
  }

  unapprove(chatId: number, userId: number, actorId: number): void {
    this.context.atomic(() => administration.unapprove(this.context, chatId, userId, actorId));
  }

  chatEnabled(chatId: number): boolean {
    return queries.chatEnabled(this.context, chatId);
  }

  toggleChat(chatId: number, enabled?: boolean): boolean {
    return this.context.atomic(() => administration.toggleChat(this.context, chatId, enabled));
  }

  override(decisionId: string, actorId: number): void {
    this.context.atomic(() => actions.overrideDecision(this.context, decisionId, actorId));
  }

  claimClassification(): ClassificationJob | undefined {
    return this.context.atomic(() => classification.claimClassification(this.context));
  }

  completeClassification(job: ClassificationJob, result: Classification): boolean {
    return this.context.atomic(() =>
      classification.completeClassification(this.context, job, result),
    );
  }

  failClassification(job: ClassificationJob, error: AppError): void {
    this.context.atomic(() => classification.failClassification(this.context, job, error));
  }

  interruptClassification(job: ClassificationJob): void {
    this.failClassification(
      job,
      new AppError({ category: "transient", message: "classification interrupted" }),
    );
  }

  claimAction(): ActionJob | undefined {
    return this.context.atomic(() => actions.claimAction(this.context));
  }

  /** Called after the membership await, immediately before the mutating request. */
  prepareAction(action: ActionJob, membership?: Membership, targetChatId?: number): boolean {
    return this.context.atomic(() =>
      actions.prepareAction(this.context, action, membership, targetChatId),
    );
  }

  completeAction(action: ActionJob, receipt?: number): void {
    this.context.atomic(() => actions.completeAction(this.context, action, receipt));
  }

  failAction(action: ActionJob, error: AppError): void {
    this.context.atomic(() => actions.failAction(this.context, action, error));
  }

  interruptAction(action: ActionJob): void {
    this.failAction(action, new AppError({ category: "unknown", message: "action interrupted" }));
  }

  status(chatId?: number): Record<string, number> {
    return queries.status(this.context, chatId);
  }

  action(id: string): ActionDetail | undefined {
    return queries.action(this.context, id);
  }

  health(): StoreHealth {
    return queries.health(this.context);
  }

  exportJournal(): number {
    return exportJournal(this.context);
  }

  /** The importer owns the source read transaction and nests this connection's write transaction. */
  importLegacy(path: string): Record<string, number> {
    return importLegacy(this.context, path);
  }
}
