from __future__ import annotations

from dataclasses import dataclass, field
from lxml.etree import Element, SubElement, tostring

from telethon.tl.types import (
    PeerChannel, PeerUser,
    MessageEntityMentionName, MessageEntityTextUrl,
    KeyboardButtonUrl, KeyboardButtonCallback,
    ReplyInlineMarkup,
    MessageFwdHeader,
)


FIELD_HINTS: dict[str, str] = {
    "sender": "sender's display name and username",
    "forwarded_from": "message was forwarded from a channel/user",
    "inline_buttons": "clickable buttons embedded in the message (often used for scam/phishing links)",
    "hidden_mentions": "invisible user mentions hidden via zero-width characters",
    "text_urls": "URLs embedded in text entities",
    "media": "attached media type",
    "via_bot": "message was sent through an inline bot",
    "post_stats": "view/forward counts (high numbers indicate viral forwarded content)",
}


@dataclass
class SenderInfo:
    name: str | None = None
    username: str | None = None


@dataclass
class ForwardInfo:
    channel_id: int | None = None
    user_id: int | None = None
    name: str | None = None
    post: int | None = None


@dataclass
class InlineButton:
    text: str
    url: str | None = None
    is_callback: bool = False


@dataclass
class MessageMetadata:
    sender: SenderInfo | None = None
    forward: ForwardInfo | None = None
    inline_buttons: list[InlineButton] = field(default_factory=list)
    hidden_mention_ids: list[int] = field(default_factory=list)
    text_urls: list[tuple[str, str]] = field(default_factory=list)  # (display_text, url)
    media_type: str | None = None
    via_bot_id: int | None = None
    views: int | None = None
    forwards: int | None = None

    @property
    def has_data(self) -> bool:
        return bool(
            self.sender
            or self.forward
            or self.inline_buttons
            or self.hidden_mention_ids
            or self.text_urls
            or self.media_type
            or self.via_bot_id is not None
            or self.views is not None
            or self.forwards is not None
        )

    def to_xml(self) -> str:
        """Build XML-like string with metadata and a legend of present fields."""
        if not self.has_data:
            return ""

        lines: list[str] = []
        fields_used: list[str] = []

        if self.sender:
            parts = []
            if self.sender.name:
                parts.append(f"name={self.sender.name}")
            if self.sender.username:
                parts.append(f"username=@{self.sender.username}")
            if parts:
                lines.append(f"<sender>{', '.join(parts)}</sender>")
                fields_used.append("sender")

        if self.forward:
            parts = []
            if self.forward.channel_id is not None:
                parts.append(f"channel_id={self.forward.channel_id}")
            if self.forward.user_id is not None:
                parts.append(f"user_id={self.forward.user_id}")
            if self.forward.name:
                parts.append(f"name={self.forward.name}")
            if self.forward.post is not None:
                parts.append(f"post={self.forward.post}")
            lines.append(f"<forwarded_from>{', '.join(parts)}</forwarded_from>")
            fields_used.append("forwarded_from")

        if self.inline_buttons:
            btn_lines = []
            for btn in self.inline_buttons:
                if btn.url:
                    btn_lines.append(f'  "{btn.text}" -> {btn.url}')
                else:
                    btn_lines.append(f'  "{btn.text}" [callback]')
            lines.append("<inline_buttons>\n" + "\n".join(btn_lines) + "\n</inline_buttons>")
            fields_used.append("inline_buttons")

        if self.hidden_mention_ids:
            ids = ", ".join(str(uid) for uid in self.hidden_mention_ids)
            lines.append(f"<hidden_mentions>user_ids: {ids}</hidden_mentions>")
            fields_used.append("hidden_mentions")

        if self.text_urls:
            url_lines = [f'  "{display}" -> {url}' for display, url in self.text_urls]
            lines.append("<text_urls>\n" + "\n".join(url_lines) + "\n</text_urls>")
            fields_used.append("text_urls")

        if self.media_type:
            lines.append(f"<media>{self.media_type}</media>")
            fields_used.append("media")

        if self.via_bot_id is not None:
            lines.append(f"<via_bot>bot_id={self.via_bot_id}</via_bot>")
            fields_used.append("via_bot")

        if self.views is not None or self.forwards is not None:
            stats = []
            if self.views is not None:
                stats.append(f"views={self.views}")
            if self.forwards is not None:
                stats.append(f"forwards={self.forwards}")
            lines.append(f"<post_stats>{', '.join(stats)}</post_stats>")
            fields_used.append("post_stats")

        # Legend: only describe fields that are actually present
        legend = "\n".join(f"- <{f}>: {FIELD_HINTS[f]}" for f in fields_used)
        lines.append(f"<metadata_legend>\n{legend}\n</metadata_legend>")

        return "<metadata>\n" + "\n".join(lines) + "\n</metadata>"


def extract_metadata(message, sender=None) -> MessageMetadata:
    """Extract metadata from a Telethon message + sender into a MessageMetadata."""
    meta = MessageMetadata()

    # Sender
    if sender:
        first = getattr(sender, 'first_name', None)
        last = getattr(sender, 'last_name', None)
        username = getattr(sender, 'username', None)
        name_parts = [p for p in (first, last) if isinstance(p, str) and p]
        name = " ".join(name_parts) if name_parts else None
        uname = username if isinstance(username, str) and username else None
        if name or uname:
            meta.sender = SenderInfo(name=name, username=uname)

    # Forward
    fwd = message.fwd_from
    if fwd and isinstance(fwd, MessageFwdHeader):
        fi = ForwardInfo()
        if fwd.from_id:
            if isinstance(fwd.from_id, PeerChannel):
                fi.channel_id = fwd.from_id.channel_id
            elif isinstance(fwd.from_id, PeerUser):
                fi.user_id = fwd.from_id.user_id
        if isinstance(getattr(fwd, 'from_name', None), str):
            fi.name = fwd.from_name
        if isinstance(getattr(fwd, 'channel_post', None), int):
            fi.post = fwd.channel_post
        meta.forward = fi

    # Inline buttons
    rm = message.reply_markup
    if rm and isinstance(rm, ReplyInlineMarkup):
        for row in rm.rows:
            for btn in row.buttons:
                if isinstance(btn, KeyboardButtonUrl):
                    meta.inline_buttons.append(InlineButton(text=btn.text, url=btn.url))
                elif isinstance(btn, KeyboardButtonCallback):
                    meta.inline_buttons.append(InlineButton(text=btn.text, is_callback=True))

    # Entities
    if message.entities and isinstance(message.entities, list):
        text = message.message or ""
        for ent in message.entities:
            if isinstance(ent, MessageEntityMentionName):
                span = text[ent.offset:ent.offset + ent.length]
                if not span.strip() or all(c in ' \u200b\u200c\u200d\u2060\ufeff' for c in span):
                    meta.hidden_mention_ids.append(ent.user_id)
            elif isinstance(ent, MessageEntityTextUrl):
                span = text[ent.offset:ent.offset + ent.length]
                meta.text_urls.append((span, ent.url))

    # Media
    if message.media:
        media_type = type(message.media).__name__
        if media_type.startswith("MessageMedia"):
            meta.media_type = media_type

    # Via bot
    via_bot = getattr(message, 'via_bot_id', None)
    if isinstance(via_bot, int):
        meta.via_bot_id = via_bot

    # View/forward stats
    views = getattr(message, 'views', None)
    if isinstance(views, int):
        meta.views = views
    forwards = getattr(message, 'forwards', None)
    if isinstance(forwards, int):
        meta.forwards = forwards

    return meta
