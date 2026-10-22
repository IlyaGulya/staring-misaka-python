import type { Prompt } from "effect/ai";
import type { Api } from "grammy";
import { AppError, type MediaRef, type Settings, type Snapshot } from "../domain.ts";
import { imageMime, loadImage, MAX_IMAGE_BYTES, type ImageData } from "../media.ts";
import { readBoundedBody } from "./http.ts";
import type { VerifiedChat } from "./reply-rule.ts";

const MAX_MESSAGES = 32;
const MAX_TEXT_LENGTH = 4000;
const MAX_METADATA_LENGTH = 6000;
const MAX_IMAGES = 8;
const MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024;

interface EvidenceRequest {
  readonly snapshot: Snapshot;
  readonly settings: Pick<Settings, "dbPath" | "telegramApiRoot" | "botToken">;
  readonly api: Api;
  readonly signal: AbortSignal;
  readonly verifiedChat?: VerifiedChat;
}

export interface PreparedEvidence {
  readonly content: readonly Prompt.UserMessagePartEncoded[];
  readonly messageIds: ReadonlySet<number>;
  readonly incomplete: boolean;
}

/** Clipping or missing media is explicit evidence incompleteness, never evidence of safety. */
export async function prepareEvidence(request: EvidenceRequest): Promise<PreparedEvidence> {
  const { snapshot, verifiedChat, signal } = request;
  const content: Prompt.UserMessagePartEncoded[] = [];
  const messageIds = new Set<number>();
  let incomplete = snapshot.messages.length > MAX_MESSAGES;
  let imageCount = 0;
  let imageBytes = 0;

  if (verifiedChat) {
    content.push({
      type: "text",
      text: JSON.stringify({
        verifiedChatId: verifiedChat.id,
        linkedChatId: verifiedChat.linkedChatId ?? null,
        note: "Replies to this chat or its linked chat are not unrelated-channel replies.",
      }),
    });
  }

  for (const message of snapshot.messages.slice(-MAX_MESSAGES)) {
    messageIds.add(message.messageId);
    const metadata = JSON.stringify(message.metadata);
    if (
      message.metadata.evidenceTruncated === true ||
      message.text.length > MAX_TEXT_LENGTH ||
      metadata.length > MAX_METADATA_LENGTH
    ) {
      incomplete = true;
    }
    content.push({
      type: "text",
      text: JSON.stringify({
        messageId: message.messageId,
        revision: message.revision,
        text: message.text.slice(0, MAX_TEXT_LENGTH),
        metadata: metadata.slice(0, MAX_METADATA_LENGTH),
      }),
    });

    for (const media of message.media) {
      let image: ImageData | undefined;
      if (imageCount < MAX_IMAGES && imageBytes < MAX_TOTAL_IMAGE_BYTES) {
        try {
          image = await loadEvidenceImage(media, request);
        } catch {
          if (signal.aborted) {
            throw new AppError({
              category: "transient",
              message: "Classification cancelled or timed out",
            });
          }
          // Failed retrieval leaves the content explicitly unavailable below.
        }
      }
      if (!image || imageBytes + image.bytes.byteLength > MAX_TOTAL_IMAGE_BYTES) {
        incomplete = true;
        content.push({
          type: "text",
          text: JSON.stringify({ messageId: message.messageId, unavailableMedia: media.kind }),
        });
        continue;
      }

      imageCount++;
      imageBytes += image.bytes.byteLength;
      content.push(
        { type: "text", text: `Image evidence for messageId ${message.messageId}:` },
        {
          type: "file",
          mediaType: image.mime,
          data: image.bytes,
        },
      );
    }
  }

  if (incomplete) {
    content.push({
      type: "text",
      text: "Some evidence is unavailable or truncated. A clean conclusion cannot establish that missing evidence is safe.",
    });
  }
  return { content, messageIds, incomplete };
}

async function loadEvidenceImage(
  media: MediaRef,
  request: EvidenceRequest,
): Promise<ImageData | undefined> {
  const { settings, api, signal } = request;
  signal.throwIfAborted();
  if (
    (media.size ?? 0) > MAX_IMAGE_BYTES ||
    (media.kind !== "photo" && media.kind !== "document")
  ) {
    return undefined;
  }
  if (
    media.mimeType &&
    !["image/jpeg", "image/png", "image/gif", "image/webp"].includes(media.mimeType)
  ) {
    return undefined;
  }
  if (media.blobHash) {
    const image = await loadImage(settings.dbPath, media.blobHash);
    signal.throwIfAborted();
    return media.mimeType && media.mimeType !== image.mime ? undefined : image;
  }
  if (!media.fileId) return undefined;

  const file = await api.getFile(media.fileId, signal);
  if (!file.file_path || (file.file_size ?? 0) > MAX_IMAGE_BYTES) return undefined;

  // A Bot API file path is relative; never follow redirects carrying the bot token.
  const segments = file.file_path.split("/");
  if (file.file_path.startsWith("/") || segments.includes("..")) return undefined;
  const path = segments.map(encodeURIComponent).join("/");
  const root = settings.telegramApiRoot.replace(/\/$/, "");
  const response = await fetch(`${root}/file/bot${settings.botToken}/${path}`, {
    signal,
    redirect: "error",
  });
  if (!response.ok) {
    await response.body?.cancel();
    return undefined;
  }

  const bytes = await readBoundedBody(response, MAX_IMAGE_BYTES);
  const mime = imageMime(bytes);
  if (!mime || (media.mimeType && media.mimeType !== mime)) return undefined;
  return { bytes, mime };
}
