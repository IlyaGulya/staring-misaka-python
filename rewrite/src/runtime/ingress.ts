import type { Update } from "grammy/types";
import type { Command, Observation } from "../domain.ts";
import { Store } from "../store.ts";
import { commandFromUpdate, normalizeUpdate } from "../telegram-observations.ts";

/** Both transports use the same tracked-chat and bot-identity boundaries. */
export class ObservationIngress {
  constructor(
    private readonly store: Store,
    private readonly botId: number,
  ) {}

  acceptUpdates(updates: readonly Update[]): void {
    if (updates.length === 0) return;

    const observations: Observation[] = [];
    const commands: Command[] = [];
    const migratedChats = new Set<number>();
    let nextOffset = 0;

    for (const update of updates) {
      for (const candidate of normalizeUpdate(update)) {
        const observation = this.trackedObservation(candidate, migratedChats);
        if (observation) observations.push(observation);
      }
      const command = commandFromUpdate(update);
      if (command) commands.push(command);
      nextOffset = Math.max(nextOffset, update.update_id + 1);
    }

    // The offset is acknowledged only with the observations and commands in the same transaction.
    this.store.acceptBatch(observations, nextOffset, commands);
  }

  observe(candidate: Observation): void {
    const observation = this.trackedObservation(candidate);
    if (observation) this.store.ingest(observation);
  }

  private trackedObservation(
    observation: Observation,
    migratedChats?: Set<number>,
  ): Observation | undefined {
    if (!this.store.tracksChat(observation.chatId) && !migratedChats?.has(observation.chatId)) {
      return undefined;
    }
    if (observation.kind === "migration" && observation.migratedTo !== undefined) {
      // Telegram may include the migration and the first new-group post in one polling batch.
      migratedChats?.add(observation.migratedTo);
    }
    if (observation.userId === this.botId) {
      return { ...observation, metadata: { ...observation.metadata, isSelf: true } };
    }
    return observation;
  }
}
