"""Unified moderation helpers for banning and purging users."""
import logging
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights

logger = logging.getLogger(__name__)


async def ban_user(client, chat_id: int, user_id: int):
    """Ban a user from a chat using the bot's admin rights.

    Args:
        client: Telethon client instance
        chat_id: Chat ID where the ban should be applied
        user_id: User ID to ban

    Raises:
        Exception: If both EditBannedRequest and kick_participant fail
    """
    rights = ChatBannedRights(
        until_date=None,
        view_messages=True,
        send_messages=True,
        send_media=True,
        send_stickers=True,
        send_gifs=True,
        send_games=True,
        send_inline=True,
        embed_links=True,
        send_polls=True,
        change_info=True,
        invite_users=True,
        pin_messages=True,
    )
    try:
        await client(EditBannedRequest(chat_id, user_id, rights))
    except Exception as e:
        # Fallback for basic groups
        try:
            await client.kick_participant(chat_id, user_id)
        except Exception:
            raise e


async def purge_user_messages(client, chat_id: int, user_id: int, count: int):
    """Delete the most recent N messages from a user in a chat.

    Args:
        client: Telethon client instance
        chat_id: Chat ID where messages should be deleted
        user_id: User ID whose messages should be deleted
        count: Number of most recent messages to delete
    """
    try:
        msgs = await client.get_messages(chat_id, from_user=user_id, limit=count)
        ids = [m.id for m in msgs]
        if ids:
            await client.delete_messages(chat_id, ids, revoke=True)
    except Exception as e:
        logger.warning(f"Failed to purge messages for user {user_id} in {chat_id}: {e}")
