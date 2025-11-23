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
        # Resolve entities explicitly before making API calls
        # This is critical for E2E tests where entity cache may be empty
        channel_entity = await client.get_input_entity(chat_id)
        user_entity = await client.get_input_entity(user_id)
        await client(EditBannedRequest(channel_entity, user_entity, rights))
    except Exception as e:
        # Fallback for basic groups
        try:
            await client.kick_participant(chat_id, user_id)
        except Exception:
            raise e


async def purge_user_messages(client, chat_id: int, user_id: int, count: int):
    """Delete the most recent N messages from a user in a chat.

    Bot-compatible version that iterates through recent messages
    instead of using the restricted SearchRequest API.

    Args:
        client: Telethon client instance
        chat_id: Chat ID where messages should be deleted
        user_id: User ID whose messages should be deleted
        count: Number of most recent messages to delete
    """
    try:
        # Resolve chat entity explicitly before iteration
        # This ensures the entity is cached for iter_messages and delete_messages
        chat_entity = await client.get_input_entity(chat_id)

        # Use iter_messages without from_user filter (bot-compatible)
        # Then manually filter by user_id
        message_ids = []

        # Iterate through recent messages (bots can do this)
        # We fetch more than needed since we'll filter by user
        async for message in client.iter_messages(chat_entity, limit=count * 10):
            if message.sender_id == user_id:
                message_ids.append(message.id)
                if len(message_ids) >= count:
                    break

        if message_ids:
            await client.delete_messages(chat_entity, message_ids, revoke=True)
            logger.info(f"Purged {len(message_ids)} messages from user {user_id} in chat {chat_id}")
    except Exception as e:
        logger.warning(f"Failed to purge messages for user {user_id} in {chat_id}: {e}")
