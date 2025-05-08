# src/staring_misaka/command_handlers.py
import datetime
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, events

from .action_service import ActionService
from .config import Settings
from .db_models import (
    GlobalBotSettings,
    LLMModel,
    ModelPricing,
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
            # Make sure client is connected before checking admin status
            if not self.client.is_connected():
                logger.warning("Telegram client not connected, cannot check group admin status.")
                return False  # Or handle appropriately
            return await is_user_admin(self.client, chat_id, user_id)
        except Exception as e:
            logger.warning(f"Could not verify group admin status for user {user_id} in chat {chat_id}: {e}")
            return False

    async def _find_prompt_by_id_or_name(self, session: AsyncSession, identifier: str) -> Prompt | None:
        try:
            prompt_id = int(identifier)
            return await session.get(Prompt, prompt_id)
        except ValueError:
            return await session.scalar(select(Prompt).where(Prompt.name == identifier))

    async def _find_model_by_id_or_name(self, session: AsyncSession, identifier: str) -> LLMModel | None:
        try:
            model_id = int(identifier)
            return await session.get(LLMModel, model_id)
        except ValueError:
            return await session.scalar(select(LLMModel).where(LLMModel.name == identifier))

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
            await session.flush()  # Ensure group exists before updating cache
            # Commit is handled by get_db_session context manager.
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
            await session.flush()  # Ensure deletion before cache update
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
            action_successful = False  # Track if the core action (e.g., ban) succeeded

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

            # Only delete the pending action if the reply was valid ('yes' or 'no')
            # AND the resulting action was processed (successfully or not for 'yes', always successful for 'no')
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
                    "  group_prompt <prompt_id|name|none> (Set custom prompt)\n"  # Added prompt/model config
                    "  group_model <model_id|name|none>   (Set custom model)"
                )
                return

            setting, value = parts[1].lower(), parts[2].lower()  # Keep original case for value if it's a name
            value_orig_case = parts[2]  # Keep original case for names/IDs

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

    # --- Prompt and Model Management Commands ---

    async def create_prompt_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.sender_id: return
        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                await event.reply("⛔ Super admin only command.")
                return
            parts = event.text.split(maxsplit=2)
            if len(parts) < 3:
                await event.reply("Usage: /create_prompt <unique_name> <prompt_text...>")
                return
            name, text = parts[1], parts[2]
            if "{message_text}" not in text:
                await event.reply("Warning: Prompt text does not contain the required '{message_text}' placeholder.")
                # Allow creation anyway, but warn
            try:
                prompt = Prompt(name=name, text=text)
                session.add(prompt)
                await session.flush()  # Get the ID
                await event.reply(f"✅ Prompt '{name}' created successfully with ID: {prompt.id}")
                logger.info(f"Prompt '{name}' (ID: {prompt.id}) created by super admin {event.sender_id}.")
            except IntegrityError:
                await event.reply(f"❌ Error: A prompt with the name '{name}' already exists.")
                await session.rollback()  # Rollback only this specific operation
            except Exception as e:
                await event.reply(f"❌ An unexpected database error occurred: {e}")
                await session.rollback()
                logger.error(f"Error creating prompt '{name}': {e}", exc_info=True)

    async def list_prompts_handler(self, event: events.NewMessage.Event):
        is_pm = event.is_private
        sender_id = event.sender_id
        chat_id = event.chat_id
        if not sender_id: return
        async with get_db_session() as session:
            is_authorised = False
            if is_pm:
                if await self._is_super_admin(session, sender_id): is_authorised = True
            elif chat_id:  # Group message
                if await self._is_group_admin_or_super_admin(session, chat_id, sender_id): is_authorised = True

            if not is_authorised:
                await event.reply("⛔ Permission denied.")
                return

            prompts_result = await session.execute(
                select(Prompt.id, Prompt.name, Prompt.is_global_default).order_by(Prompt.id))
            prompts = prompts_result.all()
            if not prompts:
                await event.reply("No prompts have been created yet.")
                return
            message_lines = ["Available Prompts (ID, Name, IsDefault):"]
            for p in prompts:
                default_marker = " <Default>" if p.is_global_default else ""
                message_lines.append(f"- ID: {p.id}, Name: `{p.name}`{default_marker}")
            await event.reply("\n".join(message_lines))

    async def set_global_prompt_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.sender_id: return
        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                await event.reply("⛔ Super admin only command.")
                return
            parts = event.text.split(maxsplit=1)
            if len(parts) < 2:
                await event.reply("Usage: /set_global_prompt <prompt_id_or_name>")
                return
            identifier = parts[1]
            prompt = await self._find_prompt_by_id_or_name(session, identifier)
            if not prompt:
                await event.reply(f"❌ Prompt '{identifier}' not found.")
                return
            gs = await session.get(GlobalBotSettings, 1)
            if not gs:
                await event.reply("Critical error: Global settings not found!")
                logger.critical("GlobalBotSettings not found during set_global_prompt")
                return

            # Unset current default if it exists and is different
            if gs.default_prompt_id and gs.default_prompt_id != prompt.id:
                current_default_prompt = await session.get(Prompt, gs.default_prompt_id)
                if current_default_prompt:
                    current_default_prompt.is_global_default = False
                    logger.info(f"Unsetting global default flag for previous prompt ID {gs.default_prompt_id}")

            prompt.is_global_default = True
            gs.default_prompt_id = prompt.id
            await event.reply(f"✅ Prompt '{prompt.name}' (ID: {prompt.id}) is now the global default.")
            logger.info(
                f"Global default prompt set to '{prompt.name}' (ID: {prompt.id}) by super admin {event.sender_id}.")

    async def set_group_prompt_handler(self, event: events.NewMessage.Event):
        # This logic is now merged into /config_group
        await event.reply("This command is deprecated. Use: `/config_group group_prompt <prompt_id|name|none>`")

    async def create_model_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.sender_id: return
        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                await event.reply("⛔ Super admin only command.")
                return
            parts = event.text.split(maxsplit=3)
            if len(parts) < 4:
                await event.reply(
                    "Usage: /create_model <unique_name> <api_identifier> <ProviderName (e.g., Anthropic, OpenAI)>")
                return
            name, api_id, provider_input = parts[1], parts[2], parts[3]
            # Case-insensitive provider matching
            provider_name_map = {"anthropic": "Anthropic", "openai": "OpenAI"}
            provider = provider_name_map.get(provider_input.lower())
            if not provider:
                await event.reply(
                    f"❌ Unsupported provider: '{provider_input}'. Supported: {', '.join(provider_name_map.keys())}.")
                return
            try:
                model = LLMModel(name=name, api_identifier=api_id, provider=provider)
                session.add(model)
                await session.flush()  # Get the ID
                await event.reply(
                    f"✅ Model '{name}' created successfully (ID: {model.id}, Provider: {provider}, API ID: {api_id})")
                logger.info(f"Model '{name}' (ID: {model.id}) created by super admin {event.sender_id}.")
            except IntegrityError:
                await event.reply(f"❌ Error: A model with the name '{name}' already exists.")
                await session.rollback()
            except Exception as e:
                await event.reply(f"❌ An unexpected database error occurred: {e}")
                await session.rollback()
                logger.error(f"Error creating model '{name}': {e}", exc_info=True)

    async def list_models_handler(self, event: events.NewMessage.Event):
        is_pm = event.is_private
        sender_id = event.sender_id
        chat_id = event.chat_id
        if not sender_id: return
        async with get_db_session() as session:
            is_authorised = False
            if is_pm:
                if await self._is_super_admin(session, sender_id): is_authorised = True
            elif chat_id:
                if await self._is_group_admin_or_super_admin(session, chat_id, sender_id): is_authorised = True

            if not is_authorised:
                await event.reply("⛔ Permission denied.")
                return

            models_result = await session.execute(
                select(LLMModel.id, LLMModel.name, LLMModel.api_identifier, LLMModel.provider).order_by(
                    LLMModel.id))
            models = models_result.all()
            if not models:
                await event.reply("No LLM models have been configured yet.")
                return
            gs = await session.get(GlobalBotSettings, 1)
            global_default_model_id = gs.default_model_id if gs else None
            message_lines = ["Available LLM Models (ID, Name, API_ID, Provider):"]
            for m in models:
                default_marker = " <Default>" if m.id == global_default_model_id else ""
                message_lines.append(
                    f"- ID: {m.id}, Name: `{m.name}`, API: `{m.api_identifier}`, Provider: {m.provider}{default_marker}")
            await event.reply("\n".join(message_lines))

    async def set_global_model_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.sender_id: return
        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                await event.reply("⛔ Super admin only command.")
                return
            parts = event.text.split(maxsplit=1)
            if len(parts) < 2:
                await event.reply("Usage: /set_global_model <model_id_or_name>")
                return
            identifier = parts[1]
            model = await self._find_model_by_id_or_name(session, identifier)
            if not model:
                await event.reply(f"❌ Model '{identifier}' not found.")
                return
            gs = await session.get(GlobalBotSettings, 1)
            if not gs:
                await event.reply("Critical error: Global settings not found!")
                logger.critical("GlobalBotSettings not found during set_global_model")
                return
            gs.default_model_id = model.id
            await event.reply(f"✅ Global default LLM model set to: '{model.name}' (ID: {model.id}).")
            logger.info(
                f"Global default model set to '{model.name}' (ID: {model.id}) by super admin {event.sender_id}.")

    async def set_group_model_handler(self, event: events.NewMessage.Event):
        # This logic is now merged into /config_group
        await event.reply("This command is deprecated. Use: `/config_group group_model <model_id|name|none>`")

    async def add_model_pricing_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.sender_id: return
        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                await event.reply("⛔ Super admin only command.")
                return
            parts = event.text.split()
            if not (6 <= len(parts) <= 7):
                await event.reply(
                    "Usage: /add_model_pricing <model_id|name> <in_price> <out_price> <CUR> <from_YYYY-MM-DD> [to_YYYY-MM-DD]")
                return
            model_identifier = parts[1]
            model = await self._find_model_by_id_or_name(session, model_identifier)
            if not model:
                await event.reply(f"❌ Model '{model_identifier}' not found.")
                return
            try:
                input_price = Decimal(parts[2])
                output_price = Decimal(parts[3])
                currency = parts[4].upper()
                from_date_str = parts[5]
                from_date = datetime.datetime.strptime(from_date_str, "%Y-%m-%d").date()
                to_date_str = parts[6] if len(parts) == 7 else None
                to_date = datetime.datetime.strptime(to_date_str, "%Y-%m-%d").date() if to_date_str else None
                if input_price < 0 or output_price < 0: raise ValueError("Prices cannot be negative.")
                if to_date and to_date < from_date: raise ValueError(
                    "Effective 'to' date cannot be before 'from' date.")
            except (ValueError, IndexError) as e:
                await event.reply(f"❌ Invalid format for price, currency, or date: {e}")
                return
            new_pricing = ModelPricing(
                model_id=model.id,
                input_price_per_million_tokens=input_price,
                output_price_per_million_tokens=output_price,
                currency=currency,
                effective_from_date=from_date,
                effective_to_date=to_date
            )
            try:
                session.add(new_pricing)
                await session.flush()  # Ensure commit succeeds before replying
                await event.reply(f"✅ Pricing added for model '{model.name}' effective from {from_date_str}.")
                logger.info(f"Pricing added for model {model.id} by super admin {event.sender_id}.")
            except IntegrityError as e:
                await event.reply(
                    f"❌ Error: Could not add pricing, possibly due to overlapping dates or other constraints: {e}")
                await session.rollback()
            except Exception as e:
                await event.reply(f"❌ An unexpected database error occurred: {e}")
                await session.rollback()
                logger.error(f"Error adding pricing for model {model.id}: {e}", exc_info=True)

    # --- Queue Management Commands ---

    async def list_queued_checks_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.sender_id: return
        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                await event.reply("⛔ Super admin only command.")
                return
            stmt = (
                select(QueuedLLMCheck)
                .where(QueuedLLMCheck.status.in_([
                    "pending_admin_action", "pending", "failed_reprocessing_attempt"
                ]))
                .order_by(QueuedLLMCheck.status.desc(), QueuedLLMCheck.queued_at.asc())
                .limit(25)
            )
            items = (await session.execute(stmt)).scalars().all()
            if not items:
                await event.reply(
                    "✅ No LLM checks currently in queue requiring attention or pending automatic retry.")
                return
            response_lines = ["Queued LLM Checks (Limit 25, Ordered by Status/Age):"]
            response_lines.append("ID | Status | Chat | User | Retries | Queued | Reason")
            response_lines.append("-" * 60)
            for item in items:
                ctx = item.message_context_json if isinstance(item.message_context_json, dict) else {}
                chat_id_str = str(ctx.get('chat_id', 'N/A'))
                user_id_str = str(ctx.get('user_id', 'N/A'))
                reason_short = item.reason_for_queueing[:60].replace('\n', ' ') + "..." if len(
                    item.reason_for_queueing) > 60 else item.reason_for_queueing.replace('\n', ' ')
                response_lines.append(
                    f"{item.id} | {item.status} | {chat_id_str} | {user_id_str} | {item.retry_count} | "
                    f"{item.queued_at.strftime('%y-%m-%d %H:%M')} | {reason_short}"
                )
            full_message = "\n".join(response_lines)
            # Avoid exceeding Telegram message limits
            if len(full_message) > 4000:
                full_message = full_message[:4000] + "\n... (message truncated)"
            await event.reply(full_message)
            if len(items) > 0:  # Only show usage if there are items
                await event.reply("Use `/reprocess_check <ID>` or `/discard_check <ID>`.")

    async def reprocess_check_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.sender_id: return
        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                await event.reply("⛔ Super admin only.")
                return
            parts = event.text.split()
            if len(parts) < 2:
                await event.reply("Usage: /reprocess_check <queued_item_id>")
                return
            try:
                item_id = int(parts[1])
            except ValueError:
                await event.reply("Invalid item ID. Must be a number.")
                return
            queued_item = await session.get(QueuedLLMCheck, item_id)
            if not queued_item:
                await event.reply(f"❌ Queued item with ID {item_id} not found.")
                return

            # Allow reprocessing pending, failed attempts, and items needing admin action
            if queued_item.status not in ["pending", "failed_reprocessing_attempt", "pending_admin_action"]:
                await event.reply(
                    f"Item {item_id} is currently '{queued_item.status}' and cannot be manually reprocessed now.")
                return

            await event.reply(f"⏳ Attempting to manually reprocess queued item ID: {item_id}...")
            # Pass the action service needed for potential admin approval requests during reprocessing
            success = await self.llm_service.reprocess_queued_item(session, item_id, self.action_service)

            # Check final status after reprocessing attempt
            final_item_state = await session.get(QueuedLLMCheck, item_id)  # Re-fetch state
            if not final_item_state:  # Item was deleted, meaning success or moved to admin action
                await event.reply(
                    f"✅ Item {item_id} successfully resolved (either processed or sent for admin approval).")
                logger.info(f"Manual reprocessing resolved item {item_id}.")
            else:  # Item still exists, means reprocessing failed critically or needs more retries/action
                status_msg = f"❌ Failed to resolve item {item_id} during manual reprocessing."
                status_msg += f" Current status: '{final_item_state.status}'. Reason: {final_item_state.reason_for_queueing}"
                await event.reply(status_msg)
                logger.warning(f"Manual reprocessing failed for item {item_id}. Status: {final_item_state.status}")

    async def discard_check_handler(self, event: events.NewMessage.Event):
        if not event.is_private or not event.sender_id: return
        async with get_db_session() as session:
            if not await self._is_super_admin(session, event.sender_id):
                await event.reply("⛔ Super admin only.")
                return
            parts = event.text.split()
            if len(parts) < 2:
                await event.reply("Usage: /discard_check <queued_item_id>")
                return
            try:
                item_id = int(parts[1])
            except ValueError:
                await event.reply("Invalid item ID. Must be a number.")
                return
            queued_item = await session.get(QueuedLLMCheck, item_id)
            if not queued_item:
                await event.reply(f"❌ Queued item {item_id} not found.")
                return

            ctx_data = queued_item.message_context_json if isinstance(queued_item.message_context_json, dict) else {}
            user_id_to_approve = ctx_data.get('user_id')
            chat_id_to_approve = ctx_data.get('chat_id')

            log_message = f"Discarding queued item {item_id}."
            reply_message = f"✅ Queued item {item_id} discarded."

            # If discarding, assume the user is okay for now (remove from NewUser)
            if user_id_to_approve and chat_id_to_approve:
                await self.action_service.process_user_approval(session, user_id_to_approve, chat_id_to_approve)
                log_message += f" Associated user {user_id_to_approve} in chat {chat_id_to_approve} marked as approved."
                reply_message += f" User {user_id_to_approve} marked as approved."
            else:
                log_message += " Could not determine user/chat from context to mark as approved."
                reply_message += " (Could not mark user as approved)."

            await session.delete(queued_item)
            await event.reply(reply_message)
            logger.info(f"Queued item {item_id} discarded by admin {event.sender_id}. {log_message}")

    def register_handlers(self):
        """Registers all command handlers with the Telethon client."""
        self.client.add_event_handler(self.add_group_handler, events.NewMessage(pattern=r"/add_group"))
        self.client.add_event_handler(self.remove_group_handler, events.NewMessage(pattern=r"/remove_group"))
        # Ensure admin_reply_handler comes BEFORE any generic message handler if both could match
        self.client.add_event_handler(self.admin_reply_handler, events.NewMessage(incoming=True, func=lambda
            e: e.is_private and e.reply_to_msg_id is not None))
        self.client.add_event_handler(self.config_group_handler, events.NewMessage(pattern=r"/config_group"))
        # Prompt commands
        self.client.add_event_handler(self.create_prompt_handler, events.NewMessage(pattern=r"/create_prompt"))
        self.client.add_event_handler(self.list_prompts_handler, events.NewMessage(pattern=r"/list_prompts"))
        self.client.add_event_handler(self.set_global_prompt_handler, events.NewMessage(pattern=r"/set_global_prompt"))
        self.client.add_event_handler(self.set_group_prompt_handler,
                                      events.NewMessage(pattern=r"/set_group_prompt"))  # Deprecated msg
        # Model commands
        self.client.add_event_handler(self.create_model_handler, events.NewMessage(pattern=r"/create_model"))
        self.client.add_event_handler(self.list_models_handler, events.NewMessage(pattern=r"/list_models"))
        self.client.add_event_handler(self.set_global_model_handler, events.NewMessage(pattern=r"/set_global_model"))
        self.client.add_event_handler(self.set_group_model_handler,
                                      events.NewMessage(pattern=r"/set_group_model"))  # Deprecated msg
        # Pricing commands
        self.client.add_event_handler(self.add_model_pricing_handler, events.NewMessage(pattern=r"/add_model_pricing"))
        # Queue commands
        self.client.add_event_handler(self.list_queued_checks_handler,
                                      events.NewMessage(pattern=r"/list_queued_checks"))
        self.client.add_event_handler(self.reprocess_check_handler, events.NewMessage(pattern=r"/reprocess_check"))
        self.client.add_event_handler(self.discard_check_handler, events.NewMessage(pattern=r"/discard_check"))
        logger.info("All command handlers registered.")