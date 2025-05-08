import logging

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import ChatAdminRequiredError, UserAdminInvalidError, UserNotParticipantError
from telethon.tl.types import ChannelParticipantsAdmins, User

logger = logging.getLogger(__name__)


async def get_user_display_name(user: User | None) -> str: # Allow user to be None
    if not user:
        return "Unknown User"
    if user.username:
        return f"@{user.username}"
    name = user.first_name
    if user.last_name:
        name += f" {user.last_name}"
    return name if name else "Unknown User" # Handle case where user has no name fields


async def is_user_admin(client: TelegramClient, chat_id: int, user_id: int) -> bool:
    try:
        async for admin in client.iter_participants(chat_id, filter=ChannelParticipantsAdmins):
            if admin.id == user_id:
                return True
        return False
    except Exception as e:
        logger.error(f"Error checking admin status for user {user_id} in chat {chat_id}: {e}")
        return False


async def ban_user_in_chat(
    client: TelegramClient, chat_id: int, user_id: int, reason: str | None = "Banned by bot."
):
    logger.info(f"Attempting to ban user {user_id} from chat {chat_id}. Reason: {reason}")
    try:
        await client.kick_participant(chat_id, user_id)
        logger.info(f"User {user_id} banned from chat {chat_id}.")
    except (UserAdminInvalidError, ChatAdminRequiredError):
        logger.error(f"Bot lacks admin rights to ban user {user_id} in chat {chat_id}.")
        raise
    except UserNotParticipantError:
        logger.warning(f"User {user_id} already not in chat {chat_id}, cannot ban.")
    except Exception as e:
        logger.error(f"Failed to ban user {user_id} in chat {chat_id}: {e}", exc_info=True)
        raise


async def delete_messages_in_chat(client: TelegramClient, chat_id: int, message_ids: list[int]):
    if not message_ids:
        return
    logger.info(f"Attempting to delete {len(message_ids)} messages in chat {chat_id}.")
    try:
        await client.delete_messages(chat_id, message_ids)
        logger.info(f"Successfully deleted {len(message_ids)} messages in chat {chat_id}.")
    except ChatAdminRequiredError:
        logger.error(f"Bot lacks admin rights to delete messages in chat {chat_id}.")
        # Do not re-raise here, as ban might have succeeded.
    except Exception as e:
        logger.error(f"Failed to delete messages in chat {chat_id}: {e}", exc_info=True)


async def send_message_to_chat(
    client: TelegramClient, chat_id: int, text: str, reply_to: int | None = None
) -> int | None:
    try:
        sent_message = await client.send_message(chat_id, text, reply_to=reply_to)
        logger.debug(f"Message sent to {chat_id}: '{text[:50]}...'")
        return sent_message.id if sent_message else None
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}", exc_info=True)
        return None


async def get_recent_user_messages(client: TelegramClient, chat_id: int, user_id: int, limit: int = 10) -> list[int]:
    message_ids = []
    try:
        # Fetch a bit more to ensure we get `limit` messages if some are filtered out by other criteria
        # or if the user's messages are sparse. The limit here is on messages *returned by Telethon*.
        async for message in client.iter_messages(chat_id, limit=limit, from_user=user_id):
            message_ids.append(message.id)
            if len(message_ids) >= limit: # Should be handled by Telethon's limit, but good practice
                break
        logger.debug(f"Found {len(message_ids)} recent messages for user {user_id} in chat {chat_id}.")
    except Exception as e:
        logger.error(f"Error fetching recent messages for user {user_id} in {chat_id}: {e}", exc_info=True)
    return message_ids
