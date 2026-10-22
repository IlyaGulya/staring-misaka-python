import { Effect, Layer } from "effect";
import { FetchHttpClient } from "effect/http";
import { OtlpSerialization, OtlpTracer } from "effect/observability";

/** Traces carry operation IDs, never chat text, API credentials or raw updates. */
export function withTelemetry<A, E>(
  program: Effect.Effect<A, E>,
  endpoint: string | undefined,
): Effect.Effect<A, E> {
  if (!endpoint) return program;
  let url: URL;
  try {
    url = new URL(endpoint);
  } catch {
    throw new Error("Invalid OTLP traces endpoint");
  }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password)
    throw new Error("Invalid OTLP traces endpoint");
  const layer = OtlpTracer.layer({
    url: url.toString(),
    resource: { serviceName: "staring-misaka" },
    exportInterval: "5 seconds",
    shutdownTimeout: "3 seconds",
  }).pipe(Layer.provide(Layer.mergeAll(FetchHttpClient.layer, OtlpSerialization.layerJson)));
  return program.pipe(Effect.provide(layer));
}
