import { AnthropicClient, AnthropicLanguageModel } from "@effect/ai-anthropic";
import { Duration, Effect, Layer, Redacted, Schema } from "effect";
import { AiError, LanguageModel, type Prompt } from "effect/ai";
import { FetchHttpClient } from "effect/http";
import { AppError, type Classification, type Settings } from "../domain.ts";
import { limitResponseBody } from "./http.ts";

interface ClassificationRequest {
  readonly model: string;
  readonly system: string;
  readonly content: readonly Prompt.UserMessagePartEncoded[];
  readonly evidenceIds: ReadonlySet<number>;
}

const Report = Schema.Struct({
  verdict: Schema.Literals(["spam", "clean", "uncertain"]),
  reason: Schema.String,
  evidenceMessageIds: Schema.Array(Schema.Int),
});

/** One shared provider layer. Retries belong exclusively to the durable job queue. */
export function anthropicLayer(settings: Settings): Layer.Layer<LanguageModel.LanguageModel> {
  const boundedFetch = Object.assign(
    async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) =>
      limitResponseBody(await globalThis.fetch(input, init), 256 * 1024),
    { preconnect: globalThis.fetch.preconnect },
  );
  return AnthropicLanguageModel.layer({
    model: settings.model,
    config: { max_tokens: 1024 },
  }).pipe(
    Layer.provide(
      AnthropicClient.layer({
        apiKey: Redacted.make(settings.anthropicKey),
        apiUrl: settings.anthropicUrl.replace(/\/$/, ""),
      }).pipe(
        Layer.provide(
          // OTLP also uses FetchHttpClient.layer; never reuse its unconfigured transport.
          Layer.fresh(FetchHttpClient.layer).pipe(
            Layer.provide(
              Layer.mergeAll(
                Layer.succeed(FetchHttpClient.RequestInit, { redirect: "error" }),
                Layer.succeed(FetchHttpClient.Fetch, boundedFetch),
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

export function requestClassification(
  model: LanguageModel.LanguageModel,
  request: ClassificationRequest,
): Effect.Effect<Omit<Classification, "promptVersion">, AppError> {
  return Effect.gen(function* () {
    const response = yield* model
      .generateObject({
        objectName: "report_classification",
        schema: Report,
        prompt: [
          { role: "system", content: request.system },
          { role: "user", content: request.content },
        ],
      })
      .pipe(
        Effect.mapError(classificationError),
        // SDK failures contain raw HTTP context. Keep only our sanitized outer moderation span.
        Effect.withTracerEnabled(false),
      );
    if (response.finishReason !== "stop") {
      return yield* Effect.fail(
        new AppError({
          category: "invalid",
          message: "Anthropic response incomplete or refused",
        }),
      );
    }
    const report = response.value;
    const validReason = report.reason.trim().length > 0 && report.reason.length <= 4000;
    const validEvidence = report.evidenceMessageIds.every(
      (id) => Number.isSafeInteger(id) && request.evidenceIds.has(id),
    );
    if (
      !validReason ||
      !validEvidence ||
      (report.verdict === "spam" && report.evidenceMessageIds.length === 0)
    ) {
      return yield* Effect.fail(
        new AppError({
          category: "invalid",
          message: "Anthropic report has invalid evidence or reason",
        }),
      );
    }
    const metadata = response.content.find((part) => part.type === "response-metadata");
    const tokens = response.usage;
    const usage: Record<string, number> = {};
    if (tokens.inputTokens.uncached !== undefined) usage.input_tokens = tokens.inputTokens.uncached;
    if (tokens.outputTokens.total !== undefined) usage.output_tokens = tokens.outputTokens.total;
    if (tokens.inputTokens.cacheRead !== undefined)
      usage.cache_read_input_tokens = tokens.inputTokens.cacheRead;
    if (tokens.inputTokens.cacheWrite !== undefined)
      usage.cache_creation_input_tokens = tokens.inputTokens.cacheWrite;
    return {
      ...report,
      evidenceMessageIds: [...new Set(report.evidenceMessageIds)],
      model: metadata?.modelId ?? request.model,
      usage,
    };
  });
}

/** Never persist SDK descriptions, causes, response bodies or authenticated request context. */
function classificationError(error: AiError.AiError): AppError {
  const reason = error.reason;
  const http = "http" in reason ? reason.http : undefined;
  const status = http?.response?.status;
  const invalid =
    reason._tag === "InvalidOutputError" ||
    reason._tag === "StructuredOutputError" ||
    reason._tag === "ContentPolicyError";
  let retryAfterMs =
    error.retryAfter === undefined ? undefined : Duration.toMillis(error.retryAfter);
  const retryHeader = http?.response?.headers["retry-after"];
  if (retryAfterMs === undefined && typeof retryHeader === "string") {
    retryAfterMs = /^\d+(?:\.\d+)?$/.test(retryHeader)
      ? Number(retryHeader) * 1000
      : Math.max(0, Date.parse(retryHeader) - Date.now());
  }
  return new AppError({
    category: invalid ? "invalid" : status === 408 || error.isRetryable ? "transient" : "permanent",
    message: status === undefined ? `Anthropic ${reason._tag}` : `Anthropic HTTP ${status}`,
    ...(retryAfterMs !== undefined && Number.isFinite(retryAfterMs) ? { retryAfterMs } : {}),
  });
}
