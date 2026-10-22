import { AppError } from "../domain.ts";

/** Enforce the limit while streaming, even when Content-Length is absent or incorrect. */
export async function readBoundedBody(response: Response, maxBytes: number): Promise<Uint8Array> {
  const declaredSize = Number(response.headers.get("content-length"));
  if (declaredSize > maxBytes) {
    await response.body?.cancel();
    throw new AppError({ category: "invalid", message: "Response exceeds download limit" });
  }
  if (!response.body) {
    throw new AppError({ category: "transient", message: "Response has no body" });
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;

  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;

      length += chunk.value.byteLength;
      if (length > maxBytes) {
        throw new AppError({ category: "invalid", message: "Response exceeds download limit" });
      }
      chunks.push(chunk.value);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }

  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

/** Guard Web response bytes before the SDK attaches its body-interruption lifecycle. */
export function limitResponseBody(response: Response, maxBytes: number): Response {
  if (!response.body) return response;
  let length = 0;
  const declaredSize = Number(response.headers.get("content-length"));
  const body = response.body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        length += chunk.byteLength;
        if (declaredSize > maxBytes || length > maxBytes) {
          controller.error(
            new AppError({
              category: "invalid",
              message: "Response exceeds download limit",
            }),
          );
        } else {
          controller.enqueue(chunk);
        }
      },
    }),
  );
  return new Response(body, { status: response.status, headers: response.headers });
}
