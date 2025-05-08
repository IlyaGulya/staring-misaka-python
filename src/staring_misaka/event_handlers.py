# src/staring_misaka/event_handlers.py
import datetime
import logging
from datetime import timezone  # For timezone-aware datetime objects

from telethon import TelegramClient, events
from telethon.tl.types import User as TelegramUser

from .action_service import ActionService
from .config import Settings
from .db_models import MonitoredGroup, NewUser
from .db_utils import get_db_session, get_monitored_chat_ids
from .dto import BanDetails, LLMSpamAnalysisResult, MessageContext
from .llm_service import LLMService
from .metrics_service import MESSAGES_PROCESSED, SPAM_DETECTED

logger = logging.getLogger(__name__)


class EventHandlers:
    def __init__(self, settings: Settings, client: TelegramClient, llm_service: LLMService,
                 action_service: ActionService):
        self.settings = settings
        self.client = client
        self.llm_service = llm_service
        self.action_service = action_service
        self.monitored_chats_cache: list[int] = []

    async def _is_chat_monitored(self, chat_id: int) -> bool:
        if not self.monitored_chats_cache:
            await self.update_monitored_chats_cache()
        return chat_id in self.monitored_chats_cache

    async def update_monitored_chats_cache(self):
        self.monitored_chats_cache = await get_monitored_chat_ids()
        logger.info(f"Updated monitored_chats_cache: {self.monitored_chats_cache}")

    async def chat_action_handler(self, event: events.ChatAction.Event):
        if not event.chat_id or not await self._is_chat_monitored(event.chat_id):
            return

        if (event.user_added or event.user_joined) and event.user_id:
            user_id = event.user_id
            chat_id = event.chat_id
            logger.info(f"User {user_id} joined/was added to monitored group {chat_id}")

            async with get_db_session() as session:
                existing_user_key = {"user_id": user_id, "chat_id": chat_id}
                existing_user = await session.get(NewUser, existing_user_key)

                if existing_user:
                    existing_user.join_time = datetime.datetime.now(timezone.utc)
                    logger.info(f"User {user_id} (re)joined chat {chat_id}. Updated join time in NewUser table.")
                else:
                    new_user_entry = NewUser(user_id=user_id, chat_id=chat_id) # join_time defaults to now(timezone.utc)
                    session.add(new_user_entry)
                    logger.info(f"Added new user {user_id} to NewUser table for chat {chat_id}.")
                # Session commit/rollback handled by get_db_session context manager

    async def new_message_handler(self, event: events.NewMessage.Event):
        if not event.chat_id: return

        if event.is_private or not await self._is_chat_monitored(event.chat_id):
            return

        if not event.text or not event.sender_id:
            return

        sender: TelegramUser | None = await event.get_sender()
        if not sender or sender.bot:
            return

        MESSAGES_PROCESSED.labels(chat_id=str(event.chat_id)).inc()

        async with get_db_session() as session:
            is_new_db_user = await session.get(NewUser, {"user_id": sender.id, "chat_id": event.chat_id})

            if not is_new_db_user:
                logger.debug(
                    f"Message from existing/approved user {sender.id} in chat {event.chat_id}. No LLM scan needed.")
                return

            logger.info(
                f"Processing message from new user {sender.id} in chat {event.chat_id}. Text: \"{event.text[:50]}...\"")

            message_ctx = MessageContext(
                user_id=sender.id, chat_id=event.chat_id, message_id=event.id,
                message_text=event.text, sender_username=sender.username,
                sender_first_name=sender.first_name, is_new_user=True
            )

            llm_result: LLMSpamAnalysisResult = await self.llm_service.analyze_message_for_spam(
                session, message_ctx
            )

            if llm_result.status == "success":
                if llm_result.is_spam:
                    model_name_for_metric = llm_result.model_name_used or "UnknownModel"
                    SPAM_DETECTED.labels(chat_id=str(event.chat_id), model_name=model_name_for_metric,
                                         detection_type="auto").inc()

                    # FIX: Fetch group_settings *after* confirming spam, before deciding action path
                    group_settings = await session.get(MonitoredGroup, event.chat_id)
                    if not group_settings:
                        logger.error(
                            f"Critical: MonitoredGroup settings not found for chat {event.chat_id} during spam processing.")
                        return # Cannot proceed without group settings

                    # FIX: Correctly check the require_admin_approval_for_ban flag
                    if group_settings.require_admin_approval_for_ban:
                        logger.info(
                            f"Spam detected for user {sender.id} (chat {event.chat_id}). LLM Reason: '{llm_result.reason}'. Requesting admin approval.")
                        await self.action_service.request_admin_approval_for_ban(session, message_ctx,
                                                                                 llm_result.reason or "LLM detected spam.")
                    else: # Auto-ban logic
                        logger.info(
                            f"Spam detected for user {sender.id} (chat {event.chat_id}). LLM Reason: '{llm_result.reason}'. Proceeding with automatic ban.")
                        ban_reason = f"Automatic ban: {llm_result.reason or 'LLM detected spam.'}"
                        ban_details = BanDetails(
                            user_id=sender.id, chat_id=event.chat_id, reason=ban_reason,
                            delete_message_ids=[
                                event.id] if group_settings.num_messages_to_delete_on_ban > 0 else [],
                            notify_user_reason=llm_result.reason if group_settings.pre_ban_message_enabled else None
                        )
                        await self.action_service.process_user_ban(session, ban_details, event.text[:200])
                        # session.flush() is called within process_user_ban after delete
                else: # Not spam
                    logger.info(
                        f"Message from new user {sender.id} (chat {event.chat_id}) determined NOT SPAM by LLM. Approving user.")
                    await self.action_service.process_user_approval(session, sender.id, event.chat_id)
                    # session.flush() is called within process_user_approval after delete

            elif llm_result.status == "deferred_admin_notified":
                logger.warning(
                    f"LLM check for user {sender.id} (chat {event.chat_id}, msg {event.id}) was deferred. "
                    f"Reason: {llm_result.error_message}. Admin has been notified. Message is queued for retry."
                )

            elif llm_result.status == "critical_error_no_check":
                logger.error(
                    f"LLM check for user {sender.id} (chat {event.chat_id}, msg {event.id}) failed critically and was NOT queued. "
                    f"Reason: {llm_result.error_message}. Admin should investigate immediately."
                )
            # Session commit/rollback handled by get_db_session context manager

    def register_handlers(self):
        """Registers the event handlers with the Telethon client."""
        self.client.add_event_handler(self.chat_action_handler, events.ChatAction)
        self.client.add_event_handler(self.new_message_handler, events.NewMessage)
        logger.info("Core Telegram event handlers registered.")