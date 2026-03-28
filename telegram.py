import json
import logging
from datetime import datetime, UTC
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from telethon import TelegramClient, events
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import UpdateChannelParticipant, PeerChannel

from db import NewUser, PendingBanRequest, BannedUser, AdminSettings, ApprovedUser, MessageQueue, GroupSettings
from llm import Llm
from userbot import UserBot

# Configure logging
logger = logging.getLogger(__name__)

_raw_events_file = None


def _init_raw_events(db_path: str):
    """Initialize raw events JSONL path next to the database file."""
    global _raw_events_file
    raw_dir = Path(db_path).parent / "raw_events"
    raw_dir.mkdir(exist_ok=True)
    _raw_events_file = raw_dir / "messages.jsonl"


def _dump_raw_event(event):
    """Dump raw Telegram message to JSONL file. Fire-and-forget, never raises."""
    if _raw_events_file is None:
        return
    try:
        data = {
            "ts": datetime.now(UTC).isoformat(),
            "chat_id": event.chat_id,
            "message": event.message.to_dict(),
        }
        with open(_raw_events_file, "a") as f:
            f.write(json.dumps(data, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"Failed to dump raw event: {e}")


def create_bot(session_factory: sessionmaker, llm: Llm, userbot: UserBot, config) -> TelegramClient:
    """Create a Telegram bot client.

    Args:
        session_factory: SQLAlchemy sessionmaker for creating database sessions
        llm: LLM instance
        userbot: Userbot instance
        config: Configuration object

    Returns:
        TelegramClient: Configured Telegram bot client
    """
    client = TelegramClient(config.bot_session_path, config.api_id, config.api_hash)
    logger.info("Creating Telegram bot client")
    _init_raw_events(config.db_path)

    # Initialize queue processor reference (will be set later)
    client.queue_processor = None

    # Cache: chat_id -> linked_channel_id (int, None, or _UNKNOWN on API error)
    _UNKNOWN = object()
    _linked_channel_cache = {}

    async def _get_linked_channel_id(chat_id: int):
        """Get the linked channel ID for a discussion group (cached).

        Returns:
            int: linked channel ID
            None: no linked channel (confirmed by API)
            _UNKNOWN: API error, couldn't determine
        """
        if chat_id in _linked_channel_cache:
            return _linked_channel_cache[chat_id]
        try:
            entity = await client.get_entity(chat_id)
            full = await client(GetFullChannelRequest(entity))
            linked_id = getattr(full.full_chat, 'linked_chat_id', None)
            _linked_channel_cache[chat_id] = linked_id
            if linked_id:
                logger.info(f"Chat {chat_id} is discussion group for channel {linked_id}")
            return linked_id
        except Exception as e:
            # Don't cache on error — could be transient (rate limit, disconnect).
            logger.warning(f"Could not fetch linked channel for chat {chat_id}: {e}")
            return _UNKNOWN

    def is_bot_enabled_for_chat(session: Session, chat_id: int) -> bool:
        """Check if bot is enabled for the given chat. Session must be provided."""
        group_settings = session.query(GroupSettings).filter_by(chat_id=chat_id).first()
        if group_settings:
            return group_settings.enabled
        # Default to enabled if no settings exist yet
        return True

    @client.on(events.ChatAction(chats=config.tracking_chat_ids))
    async def chat_action_handler(event):
        logger.debug(f"Chat action event received for chat {event.chat_id}")
        if event.chat_id not in config.tracking_chat_ids:
            logger.debug(f"Ignoring event from non-tracked chat: {event.chat_id}")
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
                except SQLAlchemyError as e:
                    logger.error(f"Database error adding user {user_id}: {str(e)}")
                    session.rollback()
            else:
                logger.debug("Ignoring non-user-added event")

    @client.on(events.NewMessage(chats=config.tracking_chat_ids))
    async def message_handler(event):
        logger.debug(f"New message in chat {event.chat_id}")

        # Phase 1: Check if bot is enabled (own session, close)
        with session_factory() as session:
            try:
                bot_enabled = is_bot_enabled_for_chat(session, event.chat_id)
            except SQLAlchemyError as e:
                logger.error(f"Database error when checking if bot is enabled for chat {event.chat_id}: {str(e)}")
                session.rollback()
                try:
                    bot_enabled = is_bot_enabled_for_chat(session, event.chat_id)
                except SQLAlchemyError as retry_error:
                    logger.error(f"Database error persists after rollback when checking bot enabled status: {str(retry_error)}")
                    session.rollback()
                    return

        if not bot_enabled:
            logger.debug(f"Bot disabled for chat {event.chat_id}, ignoring message")
            return

        # Phase 2: Get sender (async I/O, no session)
        sender = await event.get_sender()
        logger.debug(f"Message from user {sender.id}")

        # Phase 3: Check user status (own session, close)
        with session_factory() as session:
            try:
                approved_user = session.query(ApprovedUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
                if approved_user:
                    logger.debug(f"Message from pre-approved user {sender.id}, ignoring")
                    return

                new_user = session.query(NewUser).filter_by(user_id=sender.id, chat_id=event.chat_id).first()
            except SQLAlchemyError as e:
                logger.error(f"Database error when checking user {sender.id}: {str(e)}")
                session.rollback()
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

            is_new_user = new_user is not None

        # Phase 4: Queue message or fallback (async I/O)
        if is_new_user:
            _dump_raw_event(event)
            message_text = event.raw_text
            logger.info(f"[MONITORING] user_id={sender.id} chat_id={event.chat_id} message='{message_text[:50]}...'")

            # Fetch reply message text for LLM context (if this is a reply)
            reply_msg_text = None
            try:
                reply_msg = await event.message.get_reply_message()
                if reply_msg and reply_msg.text:
                    reply_msg_text = reply_msg.text
            except Exception as e:
                logger.debug(f"Could not fetch reply message: {e}")

            # Check for cross-channel reply spam pattern:
            # Spammers join and post short messages like "Лучший!" as replies
            # to posts from unrelated channels, not from this group.
            reply_to = event.message.reply_to
            if reply_to and getattr(reply_to, 'reply_to_peer_id', None):
                reply_peer = reply_to.reply_to_peer_id
                if isinstance(reply_peer, PeerChannel):
                    # Convert supergroup chat_id (-100XXXXXXXXXX) to channel_id
                    current_channel_id = -(event.chat_id) - 1000000000000
                    if reply_peer.channel_id != current_channel_id:
                        # Don't flag replies to the group's own linked channel.
                        # If we can't determine the linked channel (API error),
                        # skip the check and let LLM decide instead of risking a false ban.
                        linked_channel_id = await _get_linked_channel_id(event.chat_id)
                        if linked_channel_id is _UNKNOWN:
                            # Can't verify linked channel — enrich context for LLM
                            parts = [
                                "<cross_channel_reply>",
                                f"This message is a reply to a post from a different channel (ID: {reply_peer.channel_id}), "
                                f"not from this group's own channel.",
                                "</cross_channel_reply>",
                            ]
                            if reply_msg_text:
                                parts.extend([
                                    f"<original_post>{reply_msg_text}</original_post>",
                                ])
                            parts.append(f"<message>{message_text}</message>")
                            message_text = "\n".join(parts)
                            logger.info(
                                f"[CROSS-CHANNEL REPLY?] user_id={sender.id} chat_id={event.chat_id} "
                                f"reply_to_channel={reply_peer.channel_id} — linked channel unknown, forwarding to LLM"
                            )
                        elif reply_peer.channel_id != linked_channel_id:
                            logger.info(
                                f"[CROSS-CHANNEL REPLY] user_id={sender.id} chat_id={event.chat_id} "
                                f"reply_to_channel={reply_peer.channel_id} message='{message_text[:50]}...'"
                            )
                            await process_ban(
                                user_id=sender.id,
                                chat_id=event.chat_id,
                                message_id=event.id,
                                message_text=message_text,
                                is_automatic=True,
                            )
                            return

            # Add reply context for LLM classification
            if reply_msg_text and "<original_post>" not in message_text:
                message_text = f"<replying_to>{reply_msg_text}</replying_to>\n<message>{message_text}</message>"

            if client.queue_processor:
                try:
                    await client.queue_processor.add_message_to_queue(
                        user_id=sender.id,
                        chat_id=event.chat_id,
                        message_id=event.id,
                        message_text=message_text
                    )
                    logger.debug(f"Queued message from user {sender.id}")
                except Exception as e:
                    logger.error(f"Error adding message to queue for user {sender.id}: {str(e)}")
            else:
                logger.warning("Queue processor not available, falling back to direct spam check")
                try:
                    is_spam = await llm.is_spam(message_text)
                    logger.debug(f"Spam check result for user {sender.id}: {is_spam}")

                    if is_spam:
                        with session_factory() as session:
                            admin_settings = session.query(AdminSettings).first()
                            require_approval = admin_settings.require_approval if admin_settings else False

                        if require_approval:
                            await notify_admin(sender, message_text, event)
                        else:
                            await process_ban(
                                user_id=sender.id,
                                chat_id=event.chat_id,
                                message_id=event.id,
                                message_text=message_text,
                                is_automatic=True,
                            )
                    else:
                        await check_user_approval(sender.id, event.chat_id)
                except Exception as e:
                    logger.error(f"Error in fallback spam check for user {sender.id}: {str(e)}")
        else:
            logger.debug(f"Message from existing user {sender.id}, ignoring")

    @client.on(events.NewMessage(chats=config.tracking_chat_ids, pattern=r'^/approve'))
    async def approve_command_handler(event):
        # Only allow admin to use this command
        if event.sender_id != config.admin_id:
            logger.debug(f"Non-admin user {event.sender_id} tried to use /approve command")
            await event.reply("Don't touch me, baka!")
            return

        logger.info(f"[ADMIN] /approve command in chat {event.chat_id}")

        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.reply("Usage: /approve <@username or user_id>")
            return

        user_identifier = parts[1]

        approved_user = await approve_user(user_identifier, event.chat_id)

        if approved_user:
            await event.reply(f"User {approved_user['name']} (ID: {approved_user['id']}) has been approved and removed from monitoring.")
        else:
            await event.reply("User not found in monitoring list or error occurred.")

    @client.on(events.NewMessage(chats=config.tracking_chat_ids, pattern=r'^/unapprove'))
    async def unapprove_command_handler(event):
        # Only allow admin to use this command
        if event.sender_id != config.admin_id:
            logger.debug(f"Non-admin user {event.sender_id} tried to use /unapprove command")
            await event.reply("Don't touch me, baka!")
            return

        logger.info(f"[ADMIN] /unapprove command in chat {event.chat_id}")

        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.reply("Usage: /unapprove <@username or user_id>")
            return

        user_identifier = parts[1]

        removed_user = await remove_approval(user_identifier, event.chat_id)

        if removed_user:
            await event.reply(f"User {removed_user['name']} (ID: {removed_user['id']}) approval has been removed. They will be monitored for spam again.")
        else:
            await event.reply("User not found in approved list or error occurred.")

    @client.on(events.NewMessage(chats=config.tracking_chat_ids, pattern=r'^/toggle_bot'))
    async def toggle_bot_command_handler(event):
        # Only allow admin to use this command
        if event.sender_id != config.admin_id:
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

    @client.on(events.NewMessage(chats=config.tracking_chat_ids, pattern=r'^/(toggle_approval|status|queue_status|retry_failed|clear_completed)'))
    async def admin_commands_group_handler(event):
        # Only allow admin to use these commands in group chats
        if event.sender_id != config.admin_id:
            logger.debug(f"Non-admin user {event.sender_id} tried to use admin command: {event.raw_text}")
            await event.reply("Don't touch me, baka!")
            return

        # Admin is using command in group - redirect to private chat
        await event.reply("Please use admin commands in private chat with me.")

    async def notify_admin(sender, message_text, event):
        """Notify admin about potential spam. Opens its own session."""
        logger.info(f"[SPAM] Requesting admin approval for user_id={sender.id} chat_id={event.chat_id}")

        # Async I/O first — no session
        admin_message = (
            f"User {sender.first_name} ({sender.id}) sent a message in chat {event.chat_id}:\n\n"
            f"{message_text}\n\nShould I ban this user? Reply 'yes' to ban."
        )
        sent_message = await client.send_message(config.admin_id, admin_message)
        logger.debug(f"Admin notification sent with message ID: {sent_message.id}")

        # DB write — own session
        with session_factory() as session:
            pending_request = PendingBanRequest(
                admin_message_id=sent_message.id,
                sender_id=sender.id,
                original_chat_id=event.chat_id,
                original_message_id=event.id,
                message_text=message_text,
                created_at=datetime.now(UTC)
            )
            session.add(pending_request)
            session.commit()
            logger.debug(f"Pending ban request stored for user {sender.id}")

    async def process_ban(user_id: int, chat_id: int, message_id: int, message_text: str, is_automatic: bool):
        """Process a ban for a user. Opens its own session."""
        ban_type = "automatic" if is_automatic else "manual"
        logger.info(f"[BAN] user_id={user_id} chat_id={chat_id} type={ban_type}")

        # Async I/O first — no session
        reason = f"autoban by staring misaka. message: {message_text}"
        await userbot.send_ban_command(chat_id, message_id, reason)

        user_name_str = await get_user_name(user_id)

        # DB write — own session
        with session_factory() as session:
            banned_user = BannedUser(
                user_id=user_id,
                user_name=user_name_str,
                chat_id=chat_id,
                message_text=message_text,
                banned_at=datetime.now(UTC)
            )
            session.add(banned_user)

            new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
            if new_user:
                session.delete(new_user)

            session.commit()
            logger.debug(f"Ban information stored for user {user_id}")

        # Async I/O — no session
        admin_message = (
            f"User {user_id} has been {'automatically ' if is_automatic else ''}banned "
            f"{'due to spam detection' if is_automatic else 'as per admin approval'}."
        )
        await client.send_message(config.admin_id, admin_message)

        return banned_user

    @client.on(events.NewMessage(chats=[config.admin_id], from_users=[config.admin_id]))
    async def admin_reply_handler(event):
        logger.debug(f"Received message from admin")

        if event.raw_text.startswith('/'):
            command = event.raw_text.lower().split()[0]
            if command == '/toggle_approval':
                with session_factory() as session:
                    admin_settings = session.query(AdminSettings).first()
                    admin_settings.require_approval = not admin_settings.require_approval
                    session.commit()
                    status_text = 'required' if admin_settings.require_approval else 'not required'
                await event.reply(f"Admin approval is now {status_text}")
            elif command == '/status':
                with session_factory() as session:
                    admin_settings = session.query(AdminSettings).first()
                    status_text = 'required' if admin_settings.require_approval else 'not required'
                await event.reply(f"Admin approval is currently {status_text}")
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
            # Phase 1: DB read — find pending request
            with session_factory() as session:
                pending_request = session.query(PendingBanRequest).filter_by(
                    admin_message_id=event.reply_to_msg_id
                ).first()
                if pending_request:
                    # Extract data before closing session
                    pr_sender_id = pending_request.sender_id
                    pr_chat_id = pending_request.original_chat_id
                    pr_message_id = pending_request.original_message_id
                    pr_message_text = pending_request.message_text
                    pr_id = pending_request.id
                    found_request = True
                else:
                    found_request = False

            if found_request:
                logger.debug(f"Processing admin reply for ban request: user {pr_sender_id}")
                if event.raw_text.strip().lower() == 'yes':
                    logger.info(f"[ADMIN] Ban approved for user_id={pr_sender_id}")

                    # Async I/O — no session
                    await process_ban(
                        user_id=pr_sender_id,
                        chat_id=pr_chat_id,
                        message_id=pr_message_id,
                        message_text=pr_message_text,
                        is_automatic=False,
                    )

                    # DB write — remove pending request
                    with session_factory() as session:
                        pending_request = session.query(PendingBanRequest).filter_by(id=pr_id).first()
                        if pending_request:
                            session.delete(pending_request)
                            session.commit()
                    logger.debug(f"Pending ban request removed for user {pr_sender_id}")
                else:
                    logger.info(f"[ADMIN] Ban rejected for user_id={pr_sender_id}")
                    # Async I/O — no session
                    await client.send_message(
                        config.admin_id, f"No action taken against user {pr_sender_id}."
                    )
                    # DB write — remove pending request
                    with session_factory() as session:
                        pending_request = session.query(PendingBanRequest).filter_by(id=pr_id).first()
                        if pending_request:
                            session.delete(pending_request)
                            session.commit()
            else:
                logger.debug("Admin reply not for a pending ban request")
        else:
            # Existing code for processing non-reply messages from admin
            is_spam = await llm.is_spam(event.raw_text)
            await client.send_message(config.admin_id, f"Is spam: {is_spam}")

    async def approve_user(user_identifier: str, target_chat_id: int):
        """Approve a user for a chat. Opens its own session."""
        try:
            user_id = None
            user_name = None

            # Parse user identifier - could be @username or user_id
            if user_identifier.startswith('@'):
                username = user_identifier[1:]
                try:
                    user = await client.get_entity(username)
                    user_id = user.id
                    user_name = user.username if user.username else user.first_name
                except Exception as e:
                    logger.warning(f"Could not fetch user entity by username {username}: {str(e)}")
                    logger.debug(f"Attempting database fallback for username {username}")
                    return None
            else:
                try:
                    user_id = int(user_identifier)
                    try:
                        user = await client.get_entity(user_id)
                        user_name = user.username if user.username else user.first_name
                    except Exception as e:
                        logger.warning(f"Could not fetch user entity by ID {user_id}: {str(e)}")
                        user_name = f"User_{user_id}"
                        logger.debug(f"Using fallback name for user {user_id}")
                except ValueError:
                    logger.error(f"Invalid user ID format: {user_identifier}")
                    return None

            if user_id is None:
                return None

            # DB write — own session
            with session_factory() as session:
                new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=target_chat_id).first()
                removed_count = 0
                if new_user:
                    session.delete(new_user)
                    removed_count = 1
                    logger.debug(f"Removed user {user_id} from monitoring in chat {target_chat_id}")

                existing_approval = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=target_chat_id).first()
                approved_count = 0
                if not existing_approval:
                    approved_user_obj = ApprovedUser(user_id=user_id, chat_id=target_chat_id, approved_at=datetime.now(UTC))
                    session.add(approved_user_obj)
                    approved_count = 1
                    logger.debug(f"Added user {user_id} to approved list for chat {target_chat_id}")

                if removed_count > 0 or approved_count > 0:
                    session.commit()
                    logger.info(f"[APPROVE] user_id={user_id} chat_id={target_chat_id}")

            return {"id": user_id, "name": user_name}

        except Exception as e:
            logger.error(f"Error approving user {user_identifier}: {str(e)}")
            return None

    async def remove_approval(user_identifier: str, target_chat_id: int):
        """Remove approval for a user in a chat. Opens its own session."""
        try:
            user_id = None
            user_name = None

            if user_identifier.startswith('@'):
                username = user_identifier[1:]
                try:
                    user = await client.get_entity(username)
                    user_id = user.id
                    user_name = user.username if user.username else user.first_name
                except Exception as e:
                    logger.warning(f"Could not fetch user entity by username {username}: {str(e)}")
                    logger.debug(f"Attempting database fallback for username {username}")
                    return None
            else:
                try:
                    user_id = int(user_identifier)
                    try:
                        user = await client.get_entity(user_id)
                        user_name = user.username if user.username else user.first_name
                    except Exception as e:
                        logger.warning(f"Could not fetch user entity by ID {user_id}: {str(e)}")
                        user_name = f"User_{user_id}"
                        logger.debug(f"Using fallback name for user {user_id}")
                except ValueError:
                    logger.error(f"Invalid user ID format: {user_identifier}")
                    return None

            if user_id is None:
                return None

            # DB write — own session
            with session_factory() as session:
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

    async def check_user_approval(user_id: int, chat_id: int):
        """Auto-approve users who pass spam checks. Opens its own session."""
        with session_factory() as session:
            new_user = session.query(NewUser).filter_by(user_id=user_id, chat_id=chat_id).first()
            if new_user:
                session.delete(new_user)
                logger.debug(f"User {user_id} removed from monitoring in chat {chat_id}")

                existing_approval = session.query(ApprovedUser).filter_by(user_id=user_id, chat_id=chat_id).first()
                if not existing_approval:
                    approved_user = ApprovedUser(user_id=user_id, chat_id=chat_id, approved_at=datetime.now(UTC))
                    session.add(approved_user)
                    logger.debug(f"User {user_id} added to approved list for chat {chat_id}")

                session.commit()
                logger.info(f"[AUTO-APPROVE] user_id={user_id} chat_id={chat_id}")

    logger.info("Bot setup complete")
    # Expose linked channel cache for testing
    client._linked_channel_cache = _linked_channel_cache
    # Expose handlers for tests to call directly without poking into Telethon internals.
    # This is inert in production and simplifies unit/integration tests.
    client._handlers = {
        "chat_action_handler": chat_action_handler,
        "message_handler": message_handler,
        "approve_command_handler": approve_command_handler,
        "unapprove_command_handler": unapprove_command_handler,
        "toggle_bot_command_handler": toggle_bot_command_handler,
        "admin_commands_group_handler": admin_commands_group_handler,
        "admin_reply_handler": admin_reply_handler,
        # expose helpers used by handlers when convenient to assert on behavior
        "approve_user": approve_user,
        "remove_approval": remove_approval,
        "get_user_name": get_user_name,
        "check_user_approval": check_user_approval,
        "is_bot_enabled_for_chat": is_bot_enabled_for_chat,
    }
    return client
