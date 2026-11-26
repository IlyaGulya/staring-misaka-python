import type { MediaRef } from "../domain.ts";

const INVISIBLE_OBFUSCATION = /[\uFEFF\u200B-\u200F\u2060\u202A-\u202E\u2066-\u2069]/;
const URL_OR_HANDLE = /(?:https?:\/\/|t\.me\/|telegram\.me\/|@[a-zA-Z0-9_]{4,})/i;
const CYRILLIC = /[\u0400-\u04FF]/;
const LATIN = /[A-Za-z]/;
const RISKY_ENTITY_TYPES = new Set([
  "url",
  "text_link",
  "mention",
  "text_mention",
  "email",
  "phone_number",
  "custom_emoji",
]);

export interface RevisionRiskInput {
  readonly revision: number;
  readonly text: string;
  readonly media: ReadonlyArray<MediaRef>;
  readonly metadata: Record<string, unknown>;
}

/** Only meaningful messages count toward soft reputation; short greetings do not graduate users. */
export function isSubstantiveMessage(text: string, minChars: number): boolean {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (normalized.length < minChars) return false;
  // Require enough actual letters/digits rather than repeated punctuation or emoji.
  const alnum = normalized.replace(/[^\p{L}\p{N}]/gu, "");
  return alnum.length >= Math.max(12, Math.floor(minChars * 0.6));
}

function hasMixedScriptWord(text: string): boolean {
  for (const token of text.split(/\s+/)) {
    const lettersOnly = token.replace(/[^\p{L}]/gu, "");
    if (lettersOnly.length >= 3 && CYRILLIC.test(lettersOnly) && LATIN.test(lettersOnly)) {
      return true;
    }
  }
  return false;
}

function hasRiskyEntities(metadata: Record<string, unknown>): boolean {
  const entities = metadata.entities;
  if (!Array.isArray(entities)) return false;
  for (const entity of entities) {
    if (!entity || typeof entity !== "object") continue;
    const type = (entity as Record<string, unknown>).type;
    if (typeof type === "string" && RISKY_ENTITY_TYPES.has(type.toLowerCase())) {
      return true;
    }
  }
  return false;
}

/** Soft-reputation users skip LLM only on plain, unedited, non-burst messages with zero risk signals. */
export function assessRevisionRisk(
  input: RevisionRiskInput,
  recentBurstCount: number,
): { readonly risky: boolean; readonly reasons: readonly string[] } {
  const reasons: string[] = [];
  if (input.revision > 1) reasons.push("edit");
  if (input.media.length > 0) reasons.push("media");
  if (input.metadata.forward != null) reasons.push("forward");
  if (input.metadata.externalReply != null) reasons.push("external_reply");
  if (Array.isArray(input.metadata.buttons) && input.metadata.buttons.length > 0) {
    reasons.push("buttons");
  }
  if (input.metadata.viaBot != null || input.metadata.viaBotId != null) {
    reasons.push("via_bot");
  }
  if (hasRiskyEntities(input.metadata) || URL_OR_HANDLE.test(input.text)) {
    reasons.push("link_or_mention");
  }
  if (INVISIBLE_OBFUSCATION.test(input.text) || hasMixedScriptWord(input.text)) {
    reasons.push("obfuscation");
  }
  if (recentBurstCount >= 4) {
    reasons.push("flood_burst");
  }
  return { risky: reasons.length > 0, reasons };
}
