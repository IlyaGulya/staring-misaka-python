import { Effect, Schema } from "effect";
import type { Prompt } from "effect/ai";
import { AppError, type Classification, type Settings } from "../domain.ts";
import { readBoundedBody } from "./http.ts";

export interface ClassificationRequest {
  readonly model: string;
  readonly system: string;
  readonly content: readonly Prompt.UserMessagePartEncoded[];
  readonly evidenceIds: ReadonlySet<number>;
}

const MAX_RESPONSE_BYTES = 256 * 1024;

const Report = Schema.Struct({
  verdict: Schema.Literals(["spam", "clean", "uncertain"]),
  reason: Schema.String,
  evidenceMessageIds: Schema.Array(Schema.Int),
});

const REPORT_JSON_SCHEMA = {
  type: "object",
  properties: {
    verdict: {
      type: "string",
      enum: ["spam", "clean", "uncertain"],
      description: "Classification verdict for the target message(s).",
    },
    reason: {
      type: "string",
      description: "Concise factual justification for the verdict.",
    },
    evidenceMessageIds: {
      type: "array",
      items: { type: "integer" },
      description: "Target message IDs supporting the verdict (never context-only IDs).",
    },
  },
  required: ["verdict", "reason", "evidenceMessageIds"],
  additionalProperties: false,
} as const;

interface OpenAIResponseContent {
  readonly type?: string;
  readonly text?: string;
}

interface OpenAIResponseItem {
  readonly type?: string;
  readonly content?: ReadonlyArray<OpenAIResponseContent>;
}

interface OpenAIResponsePayload {
  readonly status?: string;
  readonly model?: string;
  readonly output_text?: string;
  readonly output?: ReadonlyArray<OpenAIResponseItem>;
  readonly usage?: {
    readonly input_tokens?: number;
    readonly output_tokens?: number;
    readonly input_tokens_details?: {
      readonly cached_tokens?: number;
      readonly cache_write_tokens?: number;
    };
    readonly output_tokens_details?: {
      readonly reasoning_tokens?: number;
    };
  };
}

function responsesUrl(baseUrl: string): string {
  const clean = baseUrl.replace(/\/$/, "");
  if (clean.endsWith("/v1")) return `${clean}/responses`;
  return `${clean}/v1/responses`;
}

function toInputContent(
  parts: readonly Prompt.UserMessagePartEncoded[],
): Array<Record<string, unknown>> {
  const result: Array<Record<string, unknown>> = [];
  for (const part of parts) {
    if (part.type === "text") {
      result.push({ type: "input_text", text: part.text });
    } else if (part.type === "file") {
      const bytes =
        part.data instanceof Uint8Array
          ? Buffer.from(part.data)
          : typeof part.data === "string"
            ? Buffer.from(part.data, "base64")
            : undefined;
      if (bytes) {
        result.push({
          type: "input_image",
          image_url: `data:${part.mediaType};base64,${bytes.toString("base64")}`,
        });
      }
    }
  }
  return result;
}

function parseRetryAfter(header: string | null): number | undefined {
  if (!header) return undefined;
  const trimmed = header.trim();
  if (/^\d+(?:\.\d+)?$/.test(trimmed)) {
    const ms = Number(trimmed) * 1000;
    return Number.isFinite(ms) ? ms : undefined;
  }
  const timestamp = Date.parse(trimmed);
  if (!Number.isFinite(timestamp)) return undefined;
  return Math.max(0, timestamp - Date.now());
}

function extractOutputText(payload: OpenAIResponsePayload): {
  text: string | undefined;
  refused: boolean;
} {
  let refused = false;
  const textParts: string[] = [];
  for (const item of payload.output ?? []) {
    for (const part of item.content ?? []) {
      if (part.type === "refusal") refused = true;
      if (part.type === "output_text" && typeof part.text === "string") {
        textParts.push(part.text);
      }
    }
  }
  if (refused) return { text: undefined, refused: true };
  if (textParts.length > 0) return { text: textParts.join(""), refused: false };
  if (typeof payload.output_text === "string" && payload.output_text.length > 0) {
    return { text: payload.output_text, refused: false };
  }
  return { text: undefined, refused: false };
}

function extractUsage(raw: OpenAIResponsePayload["usage"]): Record<string, number> {
  const usage: Record<string, number> = {};
  if (!raw) return usage;
  if (typeof raw.input_tokens === "number" && raw.input_tokens >= 0) {
    usage.input_tokens = raw.input_tokens;
  }
  if (typeof raw.output_tokens === "number" && raw.output_tokens >= 0) {
    usage.output_tokens = raw.output_tokens;
  }
  const cached = raw.input_tokens_details?.cached_tokens;
  if (typeof cached === "number" && cached >= 0) {
    usage.cached_input_tokens = cached;
  }
  const written = raw.input_tokens_details?.cache_write_tokens;
  if (typeof written === "number" && written >= 0) {
    usage.cache_write_tokens = written;
  }
  const reasoning = raw.output_tokens_details?.reasoning_tokens;
  if (typeof reasoning === "number" && reasoning >= 0) {
    usage.reasoning_tokens = reasoning;
  }
  return usage;
}

/** Direct OpenAI Responses API call: no hidden retries, no redirects, bounded response body, store=false. */
export function requestOpenAIClassification(
  settings: Pick<Settings, "openaiKey" | "openaiUrl" | "reasoningEffort" | "maxOutputTokens">,
  request: ClassificationRequest,
): Effect.Effect<Omit<Classification, "promptVersion">, AppError> {
  return Effect.tryPromise({
    try: async (signal) => {
      const apiKey = settings.openaiKey ?? "";
      if (!apiKey) {
        throw new AppError({ category: "permanent", message: "OpenAI API key is not configured" });
      }
      const url = responsesUrl(settings.openaiUrl ?? "https://api.openai.com/v1");
      const body: Record<string, unknown> = {
        model: request.model,
        instructions: request.system,
        input: [{ role: "user", content: toInputContent(request.content) }],
        text: {
          format: {
            type: "json_schema",
            name: "report_classification",
            description: "Classify target Telegram message(s) as spam, clean, or uncertain.",
            strict: true,
            schema: REPORT_JSON_SCHEMA,
          },
        },
        tools: [],
        truncation: "disabled",
        store: false,
        reasoning: {
          mode: "standard",
          effort: settings.reasoningEffort ?? "low",
        },
        prompt_cache_options: {
          mode: "implicit",
          ttl: "30m",
        },
        max_output_tokens: settings.maxOutputTokens ?? 4096,
      };

      let response: Response;
      try {
        response = await fetch(url, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            authorization: `Bearer ${apiKey}`,
          },
          body: JSON.stringify(body),
          redirect: "error",
          signal,
        });
      } catch (error) {
        if (error instanceof AppError) throw error;
        throw new AppError({
          category: "transient",
          message: signal.aborted ? "OpenAI request cancelled" : "OpenAI transport failed",
        });
      }

      if (!response.ok) {
        const retryAfterMs = parseRetryAfter(response.headers.get("retry-after"));
        await response.body?.cancel().catch(() => undefined);
        const transient =
          response.status === 408 || response.status === 429 || response.status >= 500;
        throw new AppError({
          category: transient ? "transient" : "permanent",
          message: `OpenAI HTTP ${response.status}`,
          ...(retryAfterMs === undefined ? {} : { retryAfterMs }),
        });
      }

      const rawBytes = await readBoundedBody(response, MAX_RESPONSE_BYTES);
      let payload: OpenAIResponsePayload;
      try {
        payload = JSON.parse(new TextDecoder().decode(rawBytes)) as OpenAIResponsePayload;
      } catch {
        throw new AppError({ category: "invalid", message: "OpenAI response was not valid JSON" });
      }

      if (payload.status !== "completed") {
        throw new AppError({
          category: "invalid",
          message: `OpenAI response incomplete (${String(payload.status ?? "unknown")})`,
        });
      }

      const extracted = extractOutputText(payload);
      if (extracted.refused || !extracted.text) {
        throw new AppError({
          category: "invalid",
          message: "OpenAI response refused or empty",
        });
      }

      let report: typeof Report.Type;
      try {
        report = Schema.decodeUnknownSync(Report)(JSON.parse(extracted.text));
      } catch {
        throw new AppError({
          category: "invalid",
          message: "OpenAI structured output failed schema validation",
        });
      }

      const validReason = report.reason.trim().length > 0 && report.reason.length <= 4000;
      const validEvidence = report.evidenceMessageIds.every(
        (id) => Number.isSafeInteger(id) && request.evidenceIds.has(id),
      );
      if (
        !validReason ||
        !validEvidence ||
        (report.verdict === "spam" && report.evidenceMessageIds.length === 0)
      ) {
        throw new AppError({
          category: "invalid",
          message: "OpenAI report has invalid evidence or reason",
        });
      }

      return {
        ...report,
        evidenceMessageIds: [...new Set(report.evidenceMessageIds)],
        model: typeof payload.model === "string" && payload.model ? payload.model : request.model,
        usage: extractUsage(payload.usage),
      };
    },
    catch: (error) =>
      error instanceof AppError
        ? error
        : new AppError({ category: "transient", message: "OpenAI classification failed" }),
  }).pipe(Effect.withTracerEnabled(false));
}
