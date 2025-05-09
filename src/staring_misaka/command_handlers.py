# src/staring_misaka/command_handlers.py
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, events

from .action_service import ActionService
from .config import Settings
from .db_models import (
    GlobalBotSettings,
    LLMModel,
    MonitoredGroup,
    NewUser,
    PendingAdminAction,
    Prompt,
    QueuedLLMCheck,
)
from .db_utils import get_db_session
from .dto import BanDetails
from .llm_service import LLMService
from .metrics_service import SPAM_DETECTED
from .telegram_utils import is_user_admin

logger = logging.getLogger(__name__)


class CommandHandlers:
    def __init__(self, settings: Settings, client: TelegramClient, action_service: ActionService,
                 event_handlers_ref: Any, llm_service: LLMService):
        self.settings = settings
        self.client = client
        self.action_service = action_service
        self.event_handlers_ref = event_handlers_ref
        self.llm_service = llm_service

    async def _is_super_admin(self, session: AsyncSession, user_id: int) -> bool:
        gs = await session.get(GlobalBotSettings, 1)
        return gs is not None and gs.super_admin_id == user_id

    async def _is_group_admin_or_super_admin(self, session: AsyncSession, chat_id: int, user_id: int) -> bool:
        if await self._is_super_admin(session, user_id):
            return True
        try:
            if not self.client.is_connected():
                logger.warning("Telegram client not connected, cannot check group admin status.")
                return False
            return await is_user_admin(self.client, chat_id, user_id)
        except Exception as e:
            logger.warning(f"Could not verify group admin status for user {user_id} in chat {chat_id}: {e}")
            return False

    async def _find_prompt_by_id_or_name(self, session: AsyncSession, identifier: str) -> Prompt | None:
        try:
            prompt_id = int(identifier)
            return await session.get(Prompt, prompt_id)
        except ValueError:
            # If identifier is quoted, unquote it for name search
            identifier_for_name_search = identifier
            if isinstance(identifier, str) and len(identifier) >= 2 and identifier.startswith('"') and identifier.endswith('"'):
                identifier_for_name_search = identifier[1:-1]
            return await session.scalar(select(Prompt).where(Prompt.name == identifier_for_name_search))

    async def _find_model_by_id_or_name(self, session: AsyncSession, identifier: str) -> LLMModel | None:
        try:
            model_id = int(identifier)
            return await session.get(LLMModel, model_id)
        except ValueError:
            identifier_for_name_search = identifier
            if isinstance(identifier, str) and len(identifier) >= 2 and identifier.startswith('"') and identifier.endswith('"'):
                identifier_for_name_search = identifier[1:-1]
            return await session.scalar(select(LLMModel).where(LLMModel.name == identifier_for_name_search))

    async def add_group_handler(self, event: events.NewMessage.Event):
        if event.is_private:
            await event.reply("This command must be used inside the Telegram group you wish to add.")
            return

        chat_id = event.chat_id
        sender_id = event.sender_id
        if not chat_id or not sender_id:
            logger.warning("Received /add_group without chat_id or sender_id, possibly a channel post?")
            return

        async with get_db_session() as session:
            logger.info(f"Checking admin status for user {sender_id} in chat {chat_id} for /add_group")
            if not await self._is_group_admin_or_super_admin(session, chat_id, sender_id):
                logger.warning(f"User {sender_id} is not admin in chat {chat_id}. Denying /add_group.")
                await event.reply("Only group administrators can add this group for monitoring.")
                return

            existing_group = await session.get(MonitoredGroup, chat_id)
            if existing_group:
                await event.reply("This group is already being monitored by the bot.")
                return

            logger.info(f"Adding group {chat_id} for monitoring by user {sender_id}")
            new_group = MonitoredGroup(chat_id=chat_id, added_by_user_id=sender_id)
            session.add(new_group)
            await session.flush()
            await self.event_handlers_ref.update_monitored_chats_cache()
            await event.reply(
                "✅ Group successfully added for monitoring! New users' first messages will be checked.")
            logger.info(f"Group {chat_id} added for monitoring by user {sender_id}.")

    async def remove_group_handler(self, event: events.NewMessage.Event):
        if event.is_private:
            await event.reply("Use this command inside the group to remove.")
            return
        chat_id = event.chat_id
        sender_id = event.sender_id
        if not chat_id or not sender_id:
            return

        async with get_db_session() as session:
            if not await self._is_group_admin_or_super_admin(session, chat_id, sender_id):
                await event.reply("Only group administrators or the bot super admin can remove this group.")
                return

            group_to_remove = await session.get(MonitoredGroup, chat_id)
            if not group_to_remove:
                await event.reply("This group is not currently being monitored.")
                return

            logger.info(
                f"Removing group {chat_id} and cleaning associated data (NewUser, PendingAdminAction, QueuedLLMCheck)...")
            await session.execute(delete(NewUser).where(NewUser.chat_id == chat_id))
            await session.execute(delete(PendingAdminAction).where(PendingAdminAction.original_chat_id == chat_id))
            try:
                # Use json_extract for potentially better compatibility/performance if needed, but direct access often works
                await session.execute(delete(QueuedLLMCheck).where(
                    QueuedLLMCheck.message_context_json['chat_id'].as_integer() == chat_id))
            except Exception as e_queue_del:
                logger.error(
                    f"Could not fully clean QueuedLLMCheck for chat {chat_id} due to JSON query error: {e_queue_del}. Manual cleanup might be needed.")

            await session.delete(group_to_remove)
            await session.flush()
            await self.event_handlers_ref.update_monitored_chats_cache()
            await event.reply(
                "❌ Group removed from monitoring. Associated new user entries, pending actions, and queued checks have been cleared.")
            logger.info(f"Group {chat_id} removed from monitoring by user {sender_id}.")

    async def admin_reply_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.reply_to_msg_id or not event.sender_id:
            logger.debug("Ignoring non-private message or message without reply_to/sender_id in admin_reply_handler")
            return

        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                logger.debug(f"Ignoring reply from non-super admin {event.sender_id}")
                return

            pending_action = await session.scalar(
                select(PendingAdminAction).where(PendingAdminAction.admin_message_id == event.reply_to_msg_id)
            )
            if not pending_action:
                logger.debug(f"No pending action found for reply to message ID {event.reply_to_msg_id}")
                return

            decision_text = event.text.strip().lower()
            action_taken = False
            action_successful = False

            if decision_text == 'yes':
                logger.info(
                    f"Admin {event.sender_id} approved action '{pending_action.proposed_action}' for user {pending_action.user_to_act_on_id} in chat {pending_action.original_chat_id}")
                if pending_action.proposed_action == "ban":
                    ban_reason = f"Admin approved ban. Original LLM reason: {pending_action.llm_reason_for_action or 'N/A'}"
                    group_settings = await session.get(MonitoredGroup, pending_action.original_chat_id)
                    pre_ban_msg_enabled = group_settings.pre_ban_message_enabled if group_settings else False
                    num_msgs_to_delete = group_settings.num_messages_to_delete_on_ban if group_settings else 1
                    delete_ids = [pending_action.original_message_id] if num_msgs_to_delete > 0 else []

                    ban_details_dto = BanDetails(
                        user_id=pending_action.user_to_act_on_id, chat_id=pending_action.original_chat_id,
                        reason=ban_reason, delete_message_ids=delete_ids,
                        notify_user_reason=pending_action.llm_reason_for_action if pre_ban_msg_enabled else None
                    )
                    try:
                        await self.action_service.process_user_ban(session, ban_details_dto,
                                                                   pending_action.message_text_preview)
                        await event.reply(f"Ban processed for user {pending_action.user_to_act_on_id}.")
                        SPAM_DETECTED.labels(chat_id=str(pending_action.original_chat_id), model_name="AdminOverride",
                                             detection_type="admin_approved").inc()
                        action_successful = True
                    except Exception as e_ban:
                        logger.error(f"Error processing ban from admin reply: {e_ban}", exc_info=True)
                        await event.reply(f"Failed to process ban for user {pending_action.user_to_act_on_id}: {e_ban}")
                        action_successful = False  # Ban failed

                action_taken = True

            elif decision_text == 'no':
                logger.info(
                    f"Admin {event.sender_id} denied action '{pending_action.proposed_action}' for user {pending_action.user_to_act_on_id} in chat {pending_action.original_chat_id}")
                # If action denied, typically means approve the user if it was a ban proposal
                await self.action_service.process_user_approval(session, pending_action.user_to_act_on_id,
                                                                pending_action.original_chat_id)
                await event.reply(
                    f"Action denied for user {pending_action.user_to_act_on_id}. User marked as approved for now.")
                action_successful = True  # Denial is considered a successful handling
                action_taken = True

            else:  # Reply was not 'yes' or 'no'
                await event.reply("Invalid reply. Please reply with 'yes' or 'no'.")
                # Don't delete the pending action if the reply was invalid

            if action_taken:
                logger.info(
                    f"Deleting PendingAdminAction ID {pending_action.id} after processing admin reply '{decision_text}'.")
                await session.delete(pending_action)
                # Session commit is handled by get_db_session

    async def config_group_handler(self, event: events.NewMessage.Event):
        if event.is_private:
            await event.reply("Use this command inside the group to configure.")
            return
        chat_id = event.chat_id
        sender_id = event.sender_id
        if not chat_id or not sender_id:
            return

        async with get_db_session() as session:
            if not await self._is_group_admin_or_super_admin(session, chat_id, sender_id):
                await event.reply("Only group administrators or the bot super admin can configure this group.")
                return
            group = await session.get(MonitoredGroup, chat_id)
            if not group:
                await event.reply("This group is not currently monitored. Use /add_group first.")
                return

            parts = event.text.split(maxsplit=2)
            if len(parts) < 3:
                await event.reply(
                    "Usage: /config_group <setting_name> <value>\n"
                    "Available settings:\n"
                    "  approval_required <true|false>  (Require admin reply to ban?)\n"
                    "  preban_message <true|false>     (Send reason before banning?)\n"
                    "  delete_messages <true|false>    (Delete recent msgs on ban?)\n"
                    "  delete_count <number>           (How many msgs to delete? 0=none)\n"
                    "  group_prompt <prompt_id|name|none> (Set custom prompt)\n"
                    "  group_model <model_id|name|none>   (Set custom model)"
                )
                return

            setting, value = parts[1].lower(), parts[2].lower()
            value_orig_case = parts[2]

            setting_updated = False
            response_message = ""

            if setting == "approval_required":
                new_val = value == "true"
                group.require_admin_approval_for_ban = new_val
                setting_updated = True
                response_message = f"Admin approval for bans set to: {new_val}"
            elif setting == "preban_message":
                new_val = value == "true"
                group.pre_ban_message_enabled = new_val
                setting_updated = True
                response_message = f"Pre-ban notification message set to: {new_val}"
            elif setting == "delete_messages":
                new_val = value == "true"
                group.delete_recent_messages_on_ban = new_val
                setting_updated = True
                response_message = f"Deletion of recent messages on ban set to: {new_val}"
            elif setting == "delete_count":
                try:
                    count = int(value)
                    if count < 0: raise ValueError("Count cannot be negative.")
                    group.num_messages_to_delete_on_ban = count
                    setting_updated = True
                    response_message = f"Number of messages to delete on ban set to: {count}"
                except ValueError:
                    await event.reply("Invalid number for delete_count. Must be 0 or greater.")
                    return
            elif setting == "group_prompt":
                if value in ["none", "default", "reset"]:
                    group.custom_prompt_id = None
                    setting_updated = True
                    response_message = "Group prompt reset to global default."
                else:
                    prompt = await self._find_prompt_by_id_or_name(session, value_orig_case)
                    if not prompt:
                        await event.reply(f"❌ Prompt '{value_orig_case}' not found.")
                        return
                    group.custom_prompt_id = prompt.id
                    setting_updated = True
                    response_message = f"Group prompt set to: '{prompt.name}' (ID: {prompt.id})."
            elif setting == "group_model":
                if value in ["none", "default", "reset"]:
                    group.custom_model_id = None
                    setting_updated = True
                    response_message = "Group model reset to global default."
                else:
                    model = await self._find_model_by_id_or_name(session, value_orig_case)
                    if not model:
                        await event.reply(f"❌ Model '{value_orig_case}' not found.")
                        return
                    group.custom_model_id = model.id
                    setting_updated = True
                    response_message = f"Group model set to: '{model.name}' (ID: {model.id})."

            if setting_updated:
                await event.reply(f"✅ Setting Updated! {response_message}")
                logger.info(f"Group {chat_id} setting '{setting}' updated to '{value_orig_case}' by user {sender_id}.")
            else:
                await event.reply(f"❓ Unknown setting '{setting}'. See command help for available settings.")

    def register_handlers(self):
        """Registers all command handlers with the Telethon client."""
        self.client.add_event_handler(self.add_group_handler, events.NewMessage(pattern=r"/add_group"))
        self.client.add_event_handler(self.remove_group_handler, events.NewMessage(pattern=r"/remove_group"))
        # Ensure admin_reply_handler comes BEFORE any generic message handler if both could match
        self.client.add_event_handler(self.admin_reply_handler, events.NewMessage(incoming=True, func=lambda
            e: e.is_private and e.reply_to_msg_id is not None))
        self.client.add_event_handler(self.config_group_handler, events.NewMessage(pattern=r"/config_group"))

        logger.info("Command handlers registered (some moved to Web UI).")
