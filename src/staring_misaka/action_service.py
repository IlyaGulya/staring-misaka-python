# src/staring_misaka/action_service.py
import logging

from sqlalchemy import select  # Import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient

from .config import Settings
from .db_models import BannedUser, GlobalBotSettings, MonitoredGroup, NewUser, PendingAdminAction
from .dto import BanDetails, MessageContext
from .metrics_service import USERS_BANNED
from .telegram_utils import (
    ban_user_in_chat,
    delete_messages_in_chat,
    get_recent_user_messages,
    get_user_display_name,
    send_message_to_chat,
)

logger = logging.getLogger(__name__)


# TODO: Consider more granular error handling or retry mechanisms for Telegram API calls.

class ActionService:
    def __init__(self, settings: Settings, client: TelegramClient):
        self.settings = settings
        self.client = client

    async def process_user_ban(
            self, session: AsyncSession, ban_details: BanDetails, offending_message_text_sample: str | None
    ):
        """
        Handles the full ban process: sends pre-ban message, bans user, deletes messages, logs to DB.
        """
        chat_id = ban_details.chat_id
        user_id = ban_details.user_id

        group_settings = await session.get(MonitoredGroup, chat_id)
        if not group_settings:
            logger.error(f"Group {chat_id} not found in monitored groups. Cannot process ban.")
            return

        # 1. Send pre-ban message if enabled and reason provided
        if group_settings.pre_ban_message_enabled and ban_details.notify_user_reason:
            await send_message_to_chat(self.client, chat_id, ban_details.notify_user_reason)

        # 2. Collect messages to delete
        messages_to_delete = list(ban_details.delete_message_ids)  # Start with explicitly provided ones
        if (group_settings.delete_recent_messages_on_ban and
                group_settings.num_messages_to_delete_on_ban > 0 and
                not any(mid for mid in messages_to_delete)):  # Only fetch if not already provided
            recent_msg_ids = await get_recent_user_messages(
                self.client, chat_id, user_id, group_settings.num_messages_to_delete_on_ban
            )
            messages_to_delete.extend(mid for mid in recent_msg_ids if mid not in messages_to_delete)

        # Ensure the primary offending message ID is in the list if deletion is on and it's provided
        if ban_details.delete_message_ids and group_settings.num_messages_to_delete_on_ban > 0:
            primary_offending_msg_id = ban_details.delete_message_ids[0]  # Assuming first one is primary
            if primary_offending_msg_id not in messages_to_delete:
                messages_to_delete.append(primary_offending_msg_id)

        # 3. Ban user
        try:
            await ban_user_in_chat(self.client, chat_id, user_id, ban_details.reason)
        except Exception as e:
            logger.error(
                f"Failed to execute ban for user {user_id} in chat {chat_id}: {e}. Aborting further actions for this ban.")
            # Attempt to notify admin about the failure
            await send_message_to_chat(self.client, self.settings.admin_id,
                                       f"Failed to ban user {user_id} in chat {chat_id}: {e}")
            return  # Stop further processing for this ban attempt

        # 4. Delete messages (only if ban was successful)
        if messages_to_delete:
            await delete_messages_in_chat(self.client, chat_id,
                                          list(set(messages_to_delete)))  # Use set to avoid duplicates

        # 5. Record ban in DB
        # Check if user is already banned based on the unique constraint, not primary key 'id'
        stmt = select(BannedUser).where(BannedUser.user_id == user_id, BannedUser.chat_id == chat_id)
        result = await session.execute(stmt)
        existing_ban = result.scalar_one_or_none()

        if not existing_ban:
            banned_user_record = BannedUser(
                user_id=user_id,
                chat_id=chat_id,
                banned_by_user_id=(await self.client.get_me()).id,
                reason=ban_details.reason,
                original_message_text_sample=offending_message_text_sample
            )
            session.add(banned_user_record)
        else:
            # User already banned, perhaps update reason or log warning
            logger.warning(
                f"User {user_id} in chat {chat_id} is already marked as banned in the DB. Reason: '{existing_ban.reason}'. New reason: '{ban_details.reason}'")
            if existing_ban.reason != ban_details.reason:  # Optionally update details
                existing_ban.reason = ban_details.reason
                existing_ban.original_message_text_sample = offending_message_text_sample
                existing_ban.banned_by_user_id = (await self.client.get_me()).id
                # No session.add() needed as existing_ban is already tracked

        # 6. Remove from NewUser table
        # Use a dictionary for composite primary key lookup with session.get
        new_user_key = {"user_id": user_id, "chat_id": chat_id}
        new_user_record = await session.get(NewUser, new_user_key)
        if new_user_record:
            await session.delete(new_user_record)
            await session.flush()  # FIX: Added flush here

        # Determine reason_type for metric
        reason_for_metric_label = "admin_decision"  # Default
        if ban_details.reason.startswith("Automatic ban:"):
            reason_for_metric_label = "auto_spam"
        # If ban_details.reason starts with "Admin approved ban", it will correctly use the default "admin_decision".

        USERS_BANNED.labels(chat_id=str(chat_id), reason_type=reason_for_metric_label).inc()
        logger.info(f"User {user_id} successfully banned and processed in chat {chat_id}.")

        # 7. Notify super admin
        admin_notification = f"User {user_id} has been banned in chat {chat_id}. Reason: {ban_details.reason}"
        await send_message_to_chat(self.client, self.settings.admin_id, admin_notification)

    async def request_admin_approval_for_ban(
            self, session: AsyncSession, context: MessageContext, llm_reason: str
    ):
        """Requests admin approval for a ban and creates a PendingAdminAction."""
        global_settings = await session.get(GlobalBotSettings, 1)
        if not global_settings:
            logger.error("Global settings not found, cannot determine admin for approval.")
            return

        admin_to_notify = global_settings.super_admin_id

        user_entity = await self.client.get_entity(context.user_id)
        user_name_display = await get_user_display_name(user_entity)

        admin_message_text = (
            f"Potential Spam Alert:\n"
            f"User: {user_name_display} (ID: {context.user_id})\n"
            f"Chat ID: {context.chat_id}\n"
            f"Message: \"{context.message_text[:200]}...\"\n"
            f"LLM Reason: {llm_reason}\n\n"
            f"Reply to this message with 'yes' to ban, or 'no' to ignore."
        )

        # The mock client's send_message now returns a MagicMock with an 'id'
        sent_admin_message = await self.client.send_message(admin_to_notify, admin_message_text)
        sent_admin_message_id = sent_admin_message.id if sent_admin_message else None

        if sent_admin_message_id:
            pending_action = PendingAdminAction(
                admin_message_id=sent_admin_message_id,
                user_to_act_on_id=context.user_id,
                original_chat_id=context.chat_id,
                original_message_id=context.message_id,
                message_text_preview=context.message_text[:500],
                proposed_action="ban",
                llm_reason_for_action=llm_reason
            )
            session.add(pending_action)
            await session.flush()  # Make pending_action ID available if needed by subsequent code in the same transaction
            logger.info(
                f"Pending ban approval requested for user {context.user_id} in chat {context.chat_id}. Admin notified via message {sent_admin_message_id}.")
        else:
            logger.error(f"Failed to send admin notification for user {context.user_id}. Ban approval cannot proceed.")

    async def process_user_approval(self, session: AsyncSession, user_id: int, chat_id: int):
        """Mark user as 'not new' anymore in the context of a chat."""
        # Use a dictionary for composite primary key lookup with session.get
        new_user_key = {"user_id": user_id, "chat_id": chat_id}
        new_user_record = await session.get(NewUser, new_user_key)
        if new_user_record:
            await session.delete(new_user_record)
            await session.flush()  # FIX: Added flush here
            logger.info(f"User {user_id} approved (removed from NewUser table) in chat {chat_id}.")
        else:
            logger.debug(f"User {user_id} in chat {chat_id} was not in NewUser table or already processed.")