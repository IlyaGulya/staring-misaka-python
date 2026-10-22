import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { link, mkdir, open, unlink } from "node:fs/promises";
import { dirname, join } from "node:path";
import { AppError } from "./domain";

export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export type ImageMime = "image/jpeg" | "image/png" | "image/gif" | "image/webp";
export interface ImageData {
  bytes: Uint8Array;
  mime: ImageMime;
}

const pngSignature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

export function imageMime(bytes: Uint8Array): ImageMime | undefined {
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";

  // A Buffer view lets us inspect signatures without copying the image bytes.
  const buffer = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (buffer.byteLength >= 8 && buffer.subarray(0, 8).equals(pngSignature)) return "image/png";

  const gifHeader = buffer.toString("ascii", 0, 6);
  if (gifHeader === "GIF87a" || gifHeader === "GIF89a") return "image/gif";

  const riffHeader = buffer.toString("ascii", 0, 4);
  const webpHeader = buffer.toString("ascii", 8, 12);
  if (riffHeader === "RIFF" && webpHeader === "WEBP") return "image/webp";
  return undefined;
}

/** Reads only content-addressed regular files inside the managed database media directory. */
export async function loadImage(dbPath: string, hash: string): Promise<ImageData> {
  if (!/^[a-f0-9]{64}$/.test(hash)) {
    throw new AppError({ message: "Invalid managed media hash", category: "invalid" });
  }
  const file = await open(
    join(dirname(dbPath), "media", hash),
    constants.O_RDONLY | constants.O_NOFOLLOW,
  );
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size <= 0 || stat.size > MAX_IMAGE_BYTES) {
      throw new AppError({ message: "Invalid managed media size", category: "invalid" });
    }
    // One extra byte detects growth without allowing an unbounded readFile allocation.
    const buffer = new Uint8Array(stat.size + 1);
    let length = 0;
    while (length < buffer.byteLength) {
      const { bytesRead } = await file.read(buffer, length, buffer.byteLength - length, length);
      if (bytesRead === 0) break;
      length += bytesRead;
    }
    if (length !== stat.size) {
      throw new AppError({ message: "Managed media changed while reading", category: "invalid" });
    }
    const bytes = buffer.subarray(0, length);
    if (createHash("sha256").update(bytes).digest("hex") !== hash) {
      throw new AppError({ message: "Managed media integrity mismatch", category: "invalid" });
    }
    const mime = imageMime(bytes);
    if (!mime) {
      throw new AppError({ message: "Unsupported managed image format", category: "invalid" });
    }
    return { bytes, mime };
  } finally {
    await file.close();
  }
}

/** Publishes a complete, fsynced mode-0600 image under its SHA-256 without overwriting existing content. */
export async function persistImage(dbPath: string, bytes: Uint8Array): Promise<string> {
  if (bytes.byteLength === 0 || bytes.byteLength > MAX_IMAGE_BYTES || !imageMime(bytes)) {
    throw new AppError({ message: "Invalid observer image", category: "invalid" });
  }
  const hash = createHash("sha256").update(bytes).digest("hex");
  const directory = join(dirname(dbPath), "media");
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const temporary = join(directory, `.${randomUUID()}.tmp`);
  const file = await open(temporary, "wx", 0o600);
  try {
    try {
      await file.writeFile(bytes);
      await file.sync();
    } finally {
      await file.close();
    }
    try {
      await link(temporary, join(directory, hash));
    } catch (error) {
      if (!(error instanceof Error) || !("code" in error) || error.code !== "EEXIST") throw error;
      await loadImage(dbPath, hash);
    }
    const folder = await open(directory, constants.O_RDONLY);
    try {
      await folder.sync();
    } finally {
      await folder.close();
    }
    return hash;
  } finally {
    await unlink(temporary);
  }
}
