import logging
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from telethon import TelegramClient, events
from telethon.tl.types import UpdateChannelParticipant

from db import NewUser, PendingBanRequest, BannedUser, AdminSettings, ApprovedUser, MessageQueue, GroupSettings
from llm import Llm
from moderation import ban_user, purge_user_messages

# Configure logging
logger = logging.getLogger(__name__)


def create_bot(session_factory: sessionmaker, llm: Llm, config, *, client: Optional[TelegramClient] = None) -> TelegramClient:
    """Create a Telegram bot client.

    Args:
        session_factory: SQLAlchemy sessionmaker for creating database sessions
        llm: LLM instance
        config: Configuration object
        client: Optional pre-configured TelegramClient. If provided, handlers are registered
               on this instance. Otherwise, a new TelegramClient is created.

    Returns:
        TelegramClient: Configured Telegram bot client
    """
    client = client or TelegramClient(config.bot_session_path, config.api_id, config.api_hash)
    logger.info(f"Creating Telegram bot client (is_connected={client.is_connected()}, tracking_chats={config.tracking_chat_ids})")

    # Guard against re-registering handlers on the same client (important for E2E tests)
    if hasattr(client, '_misaka_handlers_registered'):
        logger.info("Handlers already registered on this client, skipping registration")
        client.queue_processor = None  # Reset queue processor reference
        # Update config for the current test (E2E tests use function-scoped configs)
        client._misaka_config = config
        logger.info(f"Updated client config with tracking_chat_ids={config.tracking_chat_ids}")
        return client

    # Mark that we're registering handlers
    client._misaka_handlers_registered = True

    # Store config on client for dynamic access in handlers
    client._misaka_config = config

    # Initialize queue processor reference (will be set later)
    client.queue_processor = None

    def _log_dest(chat_id: int) -> int:
        return client._misaka_config.log_channel_map.get(chat_id, client._misaka_config.admin_id)

    async def _log(chat_id: int, text: str):
        try:
            await client.send_message(_log_dest(chat_id), text)
        except Exception as e:
            logger.warning(f"Failed to log action for chat {chat_id}: {e}")

    def is_bot_enabled_for_chat(session: Session, chat_id: int) -> bool:
        """Check if bot is enabled for the given chat. Session must be provided."""
        group_settings = session.query(GroupSettings).filter_by(chat_id=chat_id).first()
        if group_settings:
            return group_settings.enabled
        # Default to enabled if no settings exist yet
        return True

    # Debug: Log ALL updates to see what we're receiving
    @client.on(events.Raw)
    async def raw_update_logger(update):
        from telethon.tl.types import UpdateChannelParticipant, UpdateChannel
        update_type = type(update).__name__

        # Extract chat_id from various update types
        chat_id = None
        if hasattr(update, 'chat_id'):
            chat_id = update.chat_id
        elif hasattr(update, 'peer'):
            peer = update.peer
            if hasattr(peer, 'channel_id'):
                chat_id = -1000000000000 - peer.channel_id
        elif hasattr(update, 'channel_id'):
            chat_id = -1000000000000 - update.channel_id

        # Log all updates, with extra detail for participant updates
        if isinstance(update, UpdateChannelParticipant):
            logger.info(f"[RAW_UPDATE] ⭐⭐⭐ UpdateChannelParticipant for chat_id={chat_id}, user_id={update.user_id}")
            logger.info(f"[RAW_UPDATE] prev_participant={type(update.prev_participant).__name__}, new_participant={type(update.new_participant).__name__}")
            logger.info(f"[RAW_UPDATE] Full update: {update}")
        elif isinstance(update, UpdateChannel):
            logger.debug(f"[RAW_UPDATE] UpdateChannel for chat_id={chat_id}")
        else:
            logger.debug(f"[RAW_UPDATE] {update_type} for chat_id={chat_id}")

    @client.on(events.ChatAction())
    async def chat_action_handler(event):
        logger.info(f"[CHAT_ACTION] ⭐⭐⭐ Chat action event received for chat {event.chat_id}, user_added={event.user_added}, user_joined={event.user_joined}, update_type={type(event.original_update).__name__}")
        logger.info(f"[CHAT_ACTION] Event details: user={event.user}, action_message={event.action_message}")
        logger.debug(f"[CHAT_ACTION_DEBUG] Full event: {event}")
        if event.chat_id not in client._misaka_config.tracking_chat_ids:
            logger.debug(f"Ignoring event from non-tracked chat: {event.chat_id}, tracking: {client._misaka_config.tracking_chat_ids}")
            return

        with session_factory() as session:
            # Check if bot is enabled for this chat
            try:
                bot_enabled = is_bot_enabled_for_chat(session, event.chat_id)
            except SQLAlchemyError as e:
                logger.error(f"Database error when checking if bot is enabled for chat {event.chat_id}: {str(e)}")
                session.rollback()
                # Try again after rollback
                try:
                    bot_enabled = is_bot_enabled_for_chat(session, event.chat_id)
                except SQLAlchemyError as retry_error:
                    logger.error(f"Database error persists after rollback when checking bot enabled status: {str(retry_error)}")
                    session.rollback()
                    return

            if not bot_enabled:
                logger.debug(f"Bot is disabled for chat {event.chat_id}, ignoring event")
                return

            # Check if a user has joined or been added to the group
            if (event.user_added or event.user_joined) and isinstance(event.original_update, UpdateChannelParticipant):
                user_id = event.user.id
                logger.info(f"[NEW USER] user_id={user_id} chat_id={event.chat_id}")

                try:
                    # Check if user is pre-approved
                    approved_user = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=event.chat_id).first()
                    if approved_user:
                        logger.debug(f"User {user_id} is pre-approved, skipping monitoring")
                        return

                    # Check if the user already exists in the new_users table
                    existing_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=event.chat_id).first()

                    if existing_user:
                        logger.debug(f"User {user_id} already exists, updating join time")
                        existing_user.join_time = datetime.now(UTC)
                    else:
                        logger.debug(f"Adding user {user_id} to monitoring")
                        new_user = NewUser(user_id=user_id, chat_id=event.chat_id, join_time=datetime.now(UTC))
                        session.add(new_user)

                    session.commit()
                    await _log(event.chat_id, f"👋 New user `{user_id}` joined; added to monitoring.")
                except SQLAlchemyError as e:
                    logger.error(f"Database error adding user {user_id}: {str(e)}")
                    session.rollback()
            else:
                logger.debug("Ignoring non-user-added event")

    @client.on(events.NewMessage())
    async def message_handler(event):
        # Check if this chat is being tracked
        if event.chat_id not in client._misaka_config.tracking_chat_ids:
            return

        logger.debug(f"New message in chat {event.chat_id}")

        with session_factory() as session:
            # Check if bot is enabled for this chat
            try:
                bot_enabled = is_bot_enabled_for_chat(session, event.chat_id)
            except SQLAlchemyError as e:
                logger.error(f"Database error when checking if bot is enabled for chat {event.chat_id}: {str(e)}")
                session.rollback()
                # Try again after rollback
                try:
                    bot_enabled = is_bot_enabled_for_chat(session, event.chat_id)
                except SQLAlchemyError as retry_error:
                    logger.error(f"Database error persists after rollback when checking bot enabled status: {str(retry_error)}")
                    session.rollback()
                    return

            if not bot_enabled:
                logger.debug(f"Bot disabled for chat {event.chat_id}, ignoring message")
                return

            sender = await event.get_sender()
            logger.debug(f"Message from user {sender.id}")

            try:
                # Check if sender is pre-approved
                approved_user = session.query(ApprovedUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
                if approved_user:
                    logger.debug(f"Message from pre-approved user {sender.id}, ignoring")
                    return

                # Check if sender is in the new_users table
                new_user = session.query(NewUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
            except SQLAlchemyError as e:
                logger.error(f"Database error when checking user {sender.id}: {str(e)}")
                session.rollback()
                # Try again after rollback
                try:
                    approved_user = session.query(ApprovedUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
                    if approved_user:
                        logger.debug(f"Message from pre-approved user {sender.id}, ignoring")
                        return
                    new_user = session.query(NewUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
                except SQLAlchemyError as retry_error:
                    logger.error(f"Database error persists after rollback for user {sender.id}: {str(retry_error)}")
                    session.rollback()
                    return
            if new_user:
                message_text = event.raw_text
                logger.info(f"[MONITORING] user_id={sender.id} chat_id={event.chat_id} message='{message_text[:50]}...'")

                # Add message to queue for processing instead of direct spam check
                if client.queue_processor:
                    client.queue_processor.add_message_to_queue(
                        user_id=sender.id,
                        chat_id=event.chat_id,
                        message_id=event.id,
                        message_text=message_text
                    )
                    logger.debug(f"Queued message from user {sender.id}")
                    await _log(event.chat_id, f"✉️ Message `{event.id}` from `{sender.id}` enqueued for spam checks.")
                else:
                    logger.warning("Queue processor not available, falling back to direct spam check")
                    # Fallback to direct spam check if queue processor is not available
                    try:
                        is_spam = await llm.is_spam(message_text)
                        logger.debug(f"Spam check result for user {sender.id}: {is_spam}")

                        admin_settings = session.query(AdminSettings).first()

                        if is_spam:
                            if admin_settings.require_approval:
                                # Notify admin
                                await notify_admin(sender, message_text, event, session)
                            else:
                                # Automatically ban the user
                                await process_ban(
                                    user_id=sender.id,
                                    chat_id=event.chat_id,
                                    message_id=event.id,
                                    message_text=message_text,
                                    is_automatic=True,
                                    session=session
                                )
                        else:
                            # If not spam, check if user should be approved
                            await check_user_approval(sender.id, event.chat_id, session)
                    except Exception as e:
                        logger.error(f"Error in fallback spam check for user {sender.id}: {str(e)}")
            else:
                logger.debug(f"Message from existing user {sender.id}, ignoring")

    # === Rose-like ban command (/sban) directly from the bot ===
    @client.on(events.NewMessage(pattern=r'^/(sban|ban)(?:\s+(.+))?'))
    async def sban_command_handler(event):
        # Check if this chat is being tracked
        if event.chat_id not in client._misaka_config.tracking_chat_ids:
            return

        # Only admin allowed
        if event.sender_id != client._misaka_config.admin_id:
            await event.reply("Don't touch me, baka!")
            return
        # Resolve target & N
        args = (event.pattern_match.group(2) or "").strip()
        n = None
        target = None
        if event.is_reply and not args:
            # reply mode; optional "/sban 15"
            reply = await event.get_reply_message()
            target = (await reply.get_sender()).id
            # message like "/sban 15"
            parts = event.raw_text.split()
            if len(parts) >= 2 and parts[1].isdigit():
                n = int(parts[1])
        else:
            # "/sban <user_or_id> [N]"
            parts = args.split()
            if not parts:
                await event.reply("Usage: reply `/sban [N]` or `/sban <@username|user_id> [N]`")
                return
            ident = parts[0]
            if len(parts) >= 2 and parts[1].isdigit():
                n = int(parts[1])
            try:
                if ident.startswith("@"):
                    target = (await client.get_entity(ident[1:])).id
                else:
                    target = (await client.get_entity(int(ident))).id
            except Exception:
                await event.reply("Couldn't resolve user. Provide @username or numeric ID, or reply to a message.")
                return
        if n is None:
            n = config.default_purge_count
        # Execute ban + purge
        try:
            await _ban_user_via_bot(event.chat_id, target)
            await _purge_user_messages(event.chat_id, target, n)

            # Record the ban in the database
            with session_factory() as session:
                # Create banned user record
                user_name = await get_user_name(target)
                banned_user = BannedUser(
                    user_id=target,
                    user_name=user_name,
                    chat_id=event.chat_id,
                    message_text=f"Manual ban via /sban command (purged {n} messages)",
                    banned_at=datetime.now(UTC)
                )
                session.add(banned_user)

                # Remove from NewUser table if present
                new_user = session.query(NewUser).filter_by(user_id=target, chat_id=event.chat_id).first()
                if new_user:
                    session.delete(new_user)
                    logger.debug(f"Removed user {target} from monitoring after /sban")

                # Remove from ApprovedUser table if present
                approved_user = session.query(ApprovedUser).filter_by(user_id=target, chat_id=event.chat_id).first()
                if approved_user:
                    session.delete(approved_user)
                    logger.debug(f"Removed user {target} from approved list after /sban")

                session.commit()
                logger.info(f"[SBAN] Recorded ban for user_id={target} chat_id={event.chat_id}")

            await event.reply(f"Banned `{target}` and purged last {n} messages.")
            await _log(event.chat_id, f"🔨 `/sban` by admin. Banned `{target}`; purged {n} messages.")
        except Exception as e:
            await event.reply(f"Ban/purge failed: {e}")
            await _log(event.chat_id, f"⚠️ `/sban` failed for `{target}`: {e}")

    @client.on(events.NewMessage(pattern=r'^/approve'))
    async def approve_command_handler(event):
        # Check if this chat is being tracked
        if event.chat_id not in client._misaka_config.tracking_chat_ids:
            return

        # Only allow admin to use this command
        if event.sender_id != client._misaka_config.admin_id:
            logger.debug(f"Non-admin user {event.sender_id} tried to use /approve command")
            await event.reply("Don't touch me, baka!")
            return

        logger.info(f"[ADMIN] /approve command in chat {event.chat_id}")

        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.reply("Usage: /approve <@username or user_id>")
            return

        user_identifier = parts[1]

        with session_factory() as session:
            approved_user = await approve_user(user_identifier, event.chat_id, session)

        if approved_user:
            await event.reply(f"User {approved_user['name']} (ID: {approved_user['id']}) has been approved and removed from monitoring.")
        else:
            await event.reply("User not found in monitoring list or error occurred.")

    @client.on(events.NewMessage(pattern=r'^/unapprove'))
    async def unapprove_command_handler(event):
        # Check if this chat is being tracked
        if event.chat_id not in client._misaka_config.tracking_chat_ids:
            return

        # Only allow admin to use this command
        if event.sender_id != client._misaka_config.admin_id:
            logger.debug(f"Non-admin user {event.sender_id} tried to use /unapprove command")
            await event.reply("Don't touch me, baka!")
            return

        logger.info(f"[ADMIN] /unapprove command in chat {event.chat_id}")

        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.reply("Usage: /unapprove <@username or user_id>")
            return

        user_identifier = parts[1]

        with session_factory() as session:
            removed_user = await remove_approval(user_identifier, event.chat_id, session)

        if removed_user:
            await event.reply(f"User {removed_user['name']} (ID: {removed_user['id']}) approval has been removed. They will be monitored for spam again.")
        else:
            await event.reply("User not found in approved list or error occurred.")

    @client.on(events.NewMessage(pattern=r'^/toggle_bot'))
    async def toggle_bot_command_handler(event):
        # Check if this chat is being tracked
        if event.chat_id not in client._misaka_config.tracking_chat_ids:
            return

        # Only allow admin to use this command
        if event.sender_id != client._misaka_config.admin_id:
            logger.debug(f"Non-admin user {event.sender_id} tried to use /toggle_bot command")
            await event.reply("Don't touch me, baka!")
            return

        logger.info(f"[ADMIN] /toggle_bot command in chat {event.chat_id}")

        with session_factory() as session:
            # Get or create group settings for this chat
            group_settings = session.query(GroupSettings).filter_by(chat_id=event.chat_id).first()
            if not group_settings:
                # Create new settings entry for this chat
                group_settings = GroupSettings(chat_id=event.chat_id, enabled=True)
                session.add(group_settings)
                session.commit()

            # Toggle the enabled status
            group_settings.enabled = not group_settings.enabled
            session.commit()

            status_text = "enabled" if group_settings.enabled else "disabled"
            await event.reply(f"Bot is now {status_text} in this group.")
            logger.info(f"[CONFIG] Bot {status_text} for chat {event.chat_id}")

    @client.on(events.NewMessage(pattern=r'^/(toggle_approval|status|queue_status|retry_failed|clear_completed)'))
    async def admin_commands_group_handler(event):
        # Check if this chat is being tracked
        if event.chat_id not in client._misaka_config.tracking_chat_ids:
            return

        # Only allow admin to use these commands in group chats
        if event.sender_id != client._misaka_config.admin_id:
            logger.debug(f"Non-admin user {event.sender_id} tried to use admin command: {event.raw_text}")
            await event.reply("Don't touch me, baka!")
            return

        # Admin is using command in group - redirect to private chat
        await event.reply("Please use admin commands in private chat with me.")

    async def notify_admin(sender, message_text, event, session: Session):
        """Notify admin about potential spam. Session must be provided."""
        logger.info(f"[SPAM] Requesting admin approval for user_id={sender.id} chat_id={event.chat_id}")
        # Send a message to the admin
        admin_message = (
            f"User {sender.first_name} ({sender.id}) sent a message in chat {event.chat_id}:\n\n"
            f"{message_text}\n\nShould I ban this user? Reply 'yes' to ban."
        )
        sent_message = await client.send_message(client._misaka_config.admin_id, admin_message)
        logger.debug(f"Admin notification sent with message ID: {sent_message.id}")
        # Store the pending request in the database
        pending_request = PendingBanRequest(
            admin_message_id=sent_message.id,
            sender_id=sender.id,
            original_chat_id=event.chat_id,
            original_message_id=event.id,
            message_text=message_text,
            created_at=datetime.now(UTC)
        )
        session.add(pending_request)
        session.commit()  # Must commit here so admin_reply_handler can see it in a different session
        logger.debug(f"Pending ban request stored for user {sender.id}")

    async def process_ban(user_id: int, chat_id: int, message_id: int, message_text: str, is_automatic: bool, session: Session):
        """Process a ban for a user. Session must be provided."""
        ban_type = "automatic" if is_automatic else "manual"
        logger.info(f"[BAN] user_id={user_id} chat_id={chat_id} type={ban_type}")

        # Ban with bot and purge messages (Rose-like)
        await _ban_user_via_bot(chat_id, user_id)
        await _purge_user_messages(chat_id, user_id, client._misaka_config.default_purge_count)

        # Store the ban information in the database
        banned_user = BannedUser(
            user_id=user_id,
            user_name=await get_user_name(user_id),
            chat_id=chat_id,
            message_text=message_text,
            banned_at=datetime.now(UTC)
        )
        session.add(banned_user)

        # Remove the user from NewUser table if they're still there
        new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
        if new_user:
            session.delete(new_user)

        session.commit()
        logger.debug(f"Ban information stored for user {user_id}")

        # Notify admin about the ban
        admin_message = (
            f"User {user_id} has been {'automatically ' if is_automatic else ''}banned "
            f"{'due to spam detection' if is_automatic else 'as per admin approval'}."
        )
        logger.info(f"[PROCESS_BAN] Attempting to send ban notification to admin {client._misaka_config.admin_id}")
        logger.info(f"[PROCESS_BAN] Admin message: {admin_message}")

        try:
            sent_message = await client.send_message(client._misaka_config.admin_id, admin_message)
            logger.info(f"[PROCESS_BAN] ✓ Ban notification sent successfully to admin, message_id={sent_message.id}")
        except Exception as e:
            logger.error(f"[PROCESS_BAN] ✗ Failed to send ban notification to admin: {e}", exc_info=True)
            # Try to get more details about the peer
            try:
                peer_info = await client.get_entity(client._misaka_config.admin_id)
                logger.error(f"[PROCESS_BAN] Admin peer info: {peer_info}")
            except Exception as peer_error:
                logger.error(f"[PROCESS_BAN] Could not get admin peer info: {peer_error}")

        await _log(chat_id, f"🔨 Banned `{user_id}` and purged last {client._misaka_config.default_purge_count} messages.")

        return banned_user

    @client.on(events.NewMessage())
    async def admin_reply_handler(event):
        # Only handle messages in admin private chat
        if event.chat_id != client._misaka_config.admin_id or event.sender_id != client._misaka_config.admin_id:
            return

        logger.info(f"[ADMIN_REPLY] Received message from admin: text='{event.raw_text}', reply_to={event.reply_to_msg_id}, chat_id={event.chat_id}")

        with session_factory() as session:
            if event.raw_text.startswith('/'):
                command = event.raw_text.lower().split()[0]
                logger.debug(f"Processing command: {command}")

                # Skip /start - let the dedicated handler handle it
                if command == '/start':
                    logger.debug("Skipping /start in admin_reply_handler - will be handled by start_handler")
                    return
                if command == '/toggle_approval':
                    admin_settings = session.query(AdminSettings).first()
                    admin_settings.require_approval = not admin_settings.require_approval
                    session.commit()
                    await event.reply(
                        f"Admin approval is now {'required' if admin_settings.require_approval else 'not required'}")
                elif command == '/status':
                    admin_settings = session.query(AdminSettings).first()
                    await event.reply(
                        f"Admin approval is currently {'required' if admin_settings.require_approval else 'not required'}")
                elif command == '/queue_status':
                    if client.queue_processor:
                        status = client.queue_processor.get_queue_status()
                        status_msg = (
                            f"Queue Status:\n"
                            f"• Pending: {status['pending']}\n"
                            f"• Processing: {status['processing']}\n"
                            f"• Failed: {status['failed']}\n"
                            f"• Completed: {status['completed']}\n"
                            f"• Total: {status['total']}"
                        )
                        await event.reply(status_msg)
                    else:
                        await event.reply("Queue processor not available")
                elif command == '/retry_failed':
                    if client.queue_processor:
                        count = client.queue_processor.retry_failed_messages()
                        await event.reply(f"Reset {count} failed messages to pending status")
                    else:
                        await event.reply("Queue processor not available")
                elif command == '/clear_completed':
                    if client.queue_processor:
                        count = client.queue_processor.clear_completed_messages()
                        await event.reply(f"Cleared {count} completed messages from queue")
                    else:
                        await event.reply("Queue processor not available")
                else:
                    await event.reply("Unknown command. Available commands: /toggle_approval, /status, /queue_status, /retry_failed, /clear_completed")
            elif event.reply_to_msg_id:
                # Admin is replying to a message
                # Note: In private chats, message IDs are different for each participant,
                # so we can't rely on admin_message_id matching event.reply_to_msg_id.
                # Instead, we look for any pending ban requests and process them based on the reply content.
                logger.info(f"[ADMIN_REPLY] Message is a reply to message ID: {event.reply_to_msg_id}")

                reply_text = event.raw_text.strip().lower()

                # Get all pending ban requests (there should typically be only one)
                all_pending = session.query(PendingBanRequest).all()
                logger.info(f"[ADMIN_REPLY] Found {len(all_pending)} pending ban request(s)")

                if all_pending and reply_text in ['yes', 'no']:
                    # Process the most recent pending request
                    pending_request = all_pending[0]  # Get the first (oldest) request
                    logger.info(f"[ADMIN_REPLY] Processing pending request for user {pending_request.sender_id}, admin replied: '{reply_text}'")

                    if reply_text == 'yes':
                        logger.info(f"[ADMIN] Ban approved for user_id={pending_request.sender_id}")

                        await process_ban(
                            user_id=pending_request.sender_id,
                            chat_id=pending_request.original_chat_id,
                            message_id=pending_request.original_message_id,
                            message_text=pending_request.message_text,
                            is_automatic=False,
                            session=session
                        )

                        # Remove the pending request from the database
                        session.delete(pending_request)
                        session.commit()
                        logger.info(f"[ADMIN_REPLY] Pending ban request removed for user {pending_request.sender_id}")
                    else:  # 'no'
                        logger.info(f"[ADMIN] Ban rejected for user_id={pending_request.sender_id}")
                        await client.send_message(
                            client._misaka_config.admin_id, f"No action taken against user {pending_request.sender_id}."
                        )
                        # Remove the pending request from the database
                        session.delete(pending_request)
                        session.commit()
                else:
                    if not all_pending:
                        logger.info(f"[ADMIN_REPLY] No pending ban requests found in database")
                    else:
                        logger.info(f"[ADMIN_REPLY] Reply text '{reply_text}' is not 'yes' or 'no', ignoring")
            else:
                # Existing code for processing non-reply messages from admin
                is_spam = await llm.is_spam(event.raw_text)
                await client.send_message(client._misaka_config.admin_id, f"Is spam: {is_spam}")

    async def approve_user(user_identifier: str, target_chat_id: int, session: Session):
        """Approve a user for a chat. Session must be provided."""
        try:
            user_id = None
            user_name = None

            # Parse user identifier - could be @username or user_id
            if user_identifier.startswith('@'):
                # Username format
                username = user_identifier[1:]  # Remove @ symbol
                try:
                    user = await client.get_entity(username)
                    user_id = user.id
                    user_name = user.username if user.username else user.first_name
                except Exception as e:
                    logger.warning(f"Could not fetch user entity by username {username}: {str(e)}")
                    # Try to find user in database by searching for username in stored data
                    # This is a fallback - we'll search by user_id if possible from database
                    logger.debug(f"Attempting database fallback for username {username}")
                    return None  # Username fallback is complex, require user_id for approval
            else:
                # Assume it's a user ID
                try:
                    user_id = int(user_identifier)
                    try:
                        user = await client.get_entity(user_id)
                        user_name = user.username if user.username else user.first_name
                    except Exception as e:
                        logger.warning(f"Could not fetch user entity by ID {user_id}: {str(e)}")
                        # Continue with approval using just the user_id - entity fetching failed but we can still approve
                        user_name = f"User_{user_id}"  # Fallback name
                        logger.debug(f"Using fallback name for user {user_id}")
                except ValueError:
                    logger.error(f"Invalid user ID format: {user_identifier}")
                    return None

            if user_id is None:
                return None

            # Remove user from NewUser table for the specific chat
            new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=target_chat_id).first()
            removed_count = 0
            if new_user:
                session.delete(new_user)
                removed_count = 1
                logger.debug(f"Removed user {user_id} from monitoring in chat {target_chat_id}")

            # Add user to approved users list for the specific chat only
            existing_approval = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=target_chat_id).first()
            approved_count = 0
            if not existing_approval:
                approved_user = ApprovedUser(user_id=user_id, chat_id=target_chat_id, approved_at=datetime.now(UTC))
                session.add(approved_user)
                approved_count = 1
                logger.debug(f"Added user {user_id} to approved list for chat {target_chat_id}")

            if removed_count > 0 or approved_count > 0:
                session.commit()
                logger.info(f"[APPROVE] user_id={user_id} chat_id={target_chat_id}")
                return {"id": user_id, "name": user_name}
            else:
                logger.debug(f"User {user_id} was already approved for chat {target_chat_id}")
                return {"id": user_id, "name": user_name}

        except Exception as e:
            logger.error(f"Error approving user {user_identifier}: {str(e)}")
            return None

    async def remove_approval(user_identifier: str, target_chat_id: int, session: Session):
        """Remove approval for a user in a chat. Session must be provided."""
        try:
            user_id = None
            user_name = None

            # Parse user identifier - could be @username or user_id
            if user_identifier.startswith('@'):
                # Username format
                username = user_identifier[1:]  # Remove @ symbol
                try:
                    user = await client.get_entity(username)
                    user_id = user.id
                    user_name = user.username if user.username else user.first_name
                except Exception as e:
                    logger.warning(f"Could not fetch user entity by username {username}: {str(e)}")
                    logger.debug(f"Attempting database fallback for username {username}")
                    return None  # Username fallback is complex, require user_id for removal
            else:
                # Assume it's a user ID
                try:
                    user_id = int(user_identifier)
                    try:
                        user = await client.get_entity(user_id)
                        user_name = user.username if user.username else user.first_name
                    except Exception as e:
                        logger.warning(f"Could not fetch user entity by ID {user_id}: {str(e)}")
                        # Continue with removal using just the user_id - entity fetching failed but we can still remove
                        user_name = f"User_{user_id}"  # Fallback name
                        logger.debug(f"Using fallback name for user {user_id}")
                except ValueError:
                    logger.error(f"Invalid user ID format: {user_identifier}")
                    return None

            if user_id is None:
                return None

            # Remove user from approved users list for the specific chat
            approved_user = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=target_chat_id).first()
            if approved_user:
                session.delete(approved_user)
                session.commit()
                logger.info(f"[UNAPPROVE] user_id={user_id} chat_id={target_chat_id}")
                return {"id": user_id, "name": user_name}
            else:
                logger.debug(f"User {user_id} was not in approved list for chat {target_chat_id}")
                return None

        except Exception as e:
            logger.error(f"Error removing approval for user {user_identifier}: {str(e)}")
            return None

    async def get_user_name(user_id):
        try:
            user = await client.get_entity(user_id)
            return user.username if user.username else user.first_name
        except Exception as e:
            if "disconnected" in str(e).lower():
                logger.warning(f"Telegram client disconnected while fetching user name for user_id {user_id}, using fallback")
            else:
                logger.error(f"Error fetching user name for user_id {user_id}: {str(e)}")
            return f"User_{user_id}"

    async def _ban_user_via_bot(chat_id: int, user_id: int):
        await ban_user(client, chat_id, user_id)

    async def _purge_user_messages(chat_id: int, user_id: int, count: int):
        await purge_user_messages(client, chat_id, user_id, count)

    async def check_user_approval(user_id: int, chat_id: int, session: Session):
        """Auto-approve users who pass spam checks. Session must be provided."""
        # Auto-approve users who pass spam checks by removing from monitoring and adding to approved list
        new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
        if new_user:
            # Remove from monitoring
            session.delete(new_user)
            logger.debug(f"User {user_id} removed from monitoring in chat {chat_id}")

            # Add to approved users list
            existing_approval = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=chat_id).first()
            if not existing_approval:
                approved_user = ApprovedUser(user_id=user_id, chat_id=chat_id, approved_at=datetime.now(UTC))
                session.add(approved_user)
                logger.debug(f"User {user_id} added to approved list for chat {chat_id}")

            session.commit()
            logger.info(f"[AUTO-APPROVE] user_id={user_id} chat_id={chat_id}")
            await _log(chat_id, f"✅ Auto-approved `{user_id}` after passing spam check.")

    @client.on(events.NewMessage(pattern=r'^/start'))
    async def start_handler(event):
        """Handle /start command to establish peer relationships"""
        logger.debug(f"Received /start command from user {event.sender_id}")
        if event.sender_id == client._misaka_config.admin_id:
            # Admin starting conversation - show helpful message
            await event.reply(
                "Staring Misaka is watching.\n\n"
                "Admin commands:\n"
                "• /status - Check approval settings\n"
                "• /toggle_approval - Toggle admin approval requirement\n"
                "• /queue_status - View message queue status\n"
                "• /retry_failed - Retry failed queue items\n"
                "• /clear_completed - Clear completed queue items"
            )
        else:
            # Regular user - simple greeting
            await event.reply("Hello! I'm a spam detection bot.")
        raise events.StopPropagation()

    # Log registered handlers
    num_handlers = len(client.list_event_handlers())
    logger.info(f"Bot setup complete ({num_handlers} event handlers registered)")
    # Expose handlers for tests to call directly without poking into Telethon internals.
    # This is inert in production and simplifies unit/integration tests.
    client._handlers = {
        "chat_action_handler": chat_action_handler,
        "message_handler": message_handler,
        "sban_command_handler": sban_command_handler,
        "approve_command_handler": approve_command_handler,
        "unapprove_command_handler": unapprove_command_handler,
        "toggle_bot_command_handler": toggle_bot_command_handler,
        "admin_commands_group_handler": admin_commands_group_handler,
        "admin_reply_handler": admin_reply_handler,
        "start_handler": start_handler,
        # expose helpers used by handlers when convenient to assert on behavior
        "_ban_user_via_bot": _ban_user_via_bot,
        "_purge_user_messages": _purge_user_messages,
        "approve_user": approve_user,
        "remove_approval": remove_approval,
        "get_user_name": get_user_name,
        "check_user_approval": check_user_approval,
        "is_bot_enabled_for_chat": is_bot_enabled_for_chat,
    }
    return client
